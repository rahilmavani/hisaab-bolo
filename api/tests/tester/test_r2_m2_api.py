"""Tester round 2, M2 ledger API: edge inputs, status codes, search quality, KPI/trend
correctness against independent SQL, merge/edit semantics and cross-merchant isolation.

Spec: ARCHITECTURE.md "API surface", "Decisions/Identity" (one ledger per person, merging
moves identifiers), "Low-friction ledger" (lost-regular rule, slow-day rule), "Risks"
(trigram search); PLAN.md M2 (JWT auth -> merchant, dashboard summary, payers
search/profile/patch/merge, PATCH /transactions/{id}, transactions list).

Read-only checks use the demo merchant. Writes use the throwaway `other_merchant`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta

import pytest

from app.money import IST

from ..conftest import bearer


@pytest.fixture
def mid(db, settings, seeded):
    return db.execute(
        "select m.id from merchants m join auth.users u on u.id = m.owner_user_id where u.email = %s",
        (settings.demo_merchant_email,),
    ).fetchone()["id"]


async def status_of(api, method, path, **kw) -> int:
    """Status code, treating an unhandled app exception (what a real server turns into a 500) as 500."""
    try:
        return (await api.request(method, path, **kw)).status_code
    except Exception:  # noqa: BLE001 - ASGITransport re-raises app errors
        return 500


def app_now(db):
    return db.execute("select app_now() as n").fetchone()["n"]


# ---------------------------------------------------------------------------
# Status codes: bad input must be a 4xx, never a 500.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/dashboard/summary?date=0001-01-01",
    "/dashboard/summary?date=9999-12-31",
    "/dashboard/trend?end=0001-01-01",
    "/dashboard/trend?end=9999-12-31&grain=month",
])
async def test_extreme_dates_are_not_500(api, demo_token, path):
    code = await status_of(api, "GET", path, headers=bearer(demo_token))
    assert code != 500 and code < 500, f"{path} -> {code}"


async def test_search_with_nul_byte_is_not_500(api, demo_token):
    code = await status_of(api, "GET", "/payers", params={"q": "ram\x00"}, headers=bearer(demo_token))
    assert code < 500, f"GET /payers?q=ram%00 -> {code}"


@pytest.mark.parametrize("body", [{"display_name": "Asha\x00"}, {"notes": "note\x00"}])
async def test_patch_payer_with_nul_byte_is_not_500(api, other_merchant, body):
    code = await status_of(api, "PATCH", f"/payers/{other_merchant['payer_a']}", json=body,
                           headers=bearer(other_merchant["token"]))
    assert code < 500, f"PATCH /payers {body!r} -> {code}"


async def test_patch_transaction_with_nul_byte_is_not_500(api, other_merchant):
    code = await status_of(api, "PATCH", f"/transactions/{other_merchant['txns'][0]}",
                           json={"description": "Milk\x00"}, headers=bearer(other_merchant["token"]))
    assert code < 500, f"PATCH /transactions description with NUL -> {code}"


async def test_unsupported_methods_are_405(api, demo_token):
    h = bearer(demo_token)
    assert (await api.delete(f"/payers/{uuid.uuid4()}", headers=h)).status_code == 405
    assert (await api.put(f"/transactions/{uuid.uuid4()}", json={}, headers=h)).status_code == 405


# ---------------------------------------------------------------------------
# Transactions list: paging must be stable (no duplicates, nothing skipped).
# ---------------------------------------------------------------------------
async def test_transactions_paging_has_no_duplicates_or_gaps(api, demo_token, db, mid):
    h = bearer(demo_token)
    db.execute("analyze transactions")  # steady-state planner stats, as on the running server (a fresh seed has none)
    total = (await api.get("/transactions", params={"limit": 1}, headers=h)).json()["txn_count"]
    ids: list[str] = []
    for off in range(0, total, 7):
        page = (await api.get("/transactions", params={"limit": 7, "offset": off}, headers=h)).json()
        ids += [t["id"] for t in page["transactions"]]
    dupes = len(ids) - len(set(ids))
    assert dupes == 0, f"paging by 7 returned {dupes} duplicate rows (and skipped as many) out of {total}"


async def test_transactions_naive_times_are_ist(api, demo_token, db, mid):
    """'Times without an offset are IST': from=<today>T00:00 must be IST midnight."""
    now = app_now(db).astimezone(IST)
    start = datetime.combine(now.date() - timedelta(days=2), time(0), tzinfo=IST)
    end = start + timedelta(days=1)
    r = (await api.get("/transactions", params={"from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                                                "to": end.strftime("%Y-%m-%dT%H:%M:%S"), "limit": 500},
                       headers=bearer(demo_token))).json()
    n = db.execute("select count(*) n, coalesce(sum(amount_paise),0) s from transactions "
                   "where merchant_id = %s and paid_at >= %s and paid_at < %s", (mid, start, end)).fetchone()
    assert (r["txn_count"], r["total_paise"]) == (n["n"], n["s"])


async def test_transactions_for_another_merchants_payer_are_empty(api, demo_token, other_merchant):
    r = (await api.get("/transactions", params={"payer_id": str(other_merchant["payer_a"])},
                       headers=bearer(demo_token))).json()
    assert r["txn_count"] == 0 and r["transactions"] == []


# ---------------------------------------------------------------------------
# Search quality ("Risks": trigram search; customer search by name, UPI ID, phone)
# ---------------------------------------------------------------------------
async def _names(api, token, q, limit=5):
    r = await api.get("/payers", params={"q": q, "limit": limit}, headers=bearer(token))
    assert r.status_code == 200, (q, r.status_code)
    return [p["display_name"] for p in r.json()["payers"]]


@pytest.mark.parametrize("q", ["Ram", "ram", "Raam", " ram ", "RAM@PAYTM", "ram@paytm", "ram@",
                               "9876543210", "98765 43210", "+91 98765 43210", "+91-98765-43210",
                               "+919876543210", "098765 43210", "(+91) 98765 43210", "43210"])
async def test_search_ram_ranks_first(api, demo_token, q):
    names = await _names(api, demo_token, q)
    assert names and names[0] == "Ram", (q, names)


@pytest.mark.parametrize("q", ["+91 98765", "+91 987654", "+91-98765"])
async def test_search_partial_phone_typed_with_country_code(api, demo_token, q):
    """A merchant typing a number the Indian way ('+91 98765…') should see Ram before finishing it."""
    names = await _names(api, demo_token, q)
    assert "Ram" in names, (q, names)


@pytest.mark.parametrize("q,expected", [
    ("Sarma Traders", "Sharma Traders"), ("Mena Kapur", "Meena Kapoor"), ("priya", "Priya Caterers"),
    ("ACCOUNTS@SHARMATRADERS.IN", "Sharma Traders"), ("anita.desai@example.com", "Anita Desai"),
    ("sharmatraders@icici", "Sharma Traders"), ("9820012345", "Sharma Traders"),
])
async def test_search_fuzzy_names_and_identifiers(api, demo_token, q, expected):
    names = await _names(api, demo_token, q)
    assert names and names[0] == expected, (q, names)


async def test_search_ram_card_matches_deck(api, demo_token):
    """Deck slide 4: 'Ram · ram@paytm / ₹100 daily, ~8:30 AM · 121 payments since <date>' + Regular."""
    p = (await api.get("/payers", params={"q": "ram", "limit": 1}, headers=bearer(demo_token))).json()["payers"][0]
    assert p["vpa"] == "ram@paytm"
    assert p["habit"].startswith("₹100 daily, ~8:30 AM · 121 payments since "), p["habit"]
    assert "Regular" in p["badges"]


# ---------------------------------------------------------------------------
# Dashboard numbers vs independent SQL
# ---------------------------------------------------------------------------
async def test_past_day_kpis_match_sql(api, demo_token, db, mid):
    today = app_now(db).astimezone(IST).date()
    day = today - timedelta(days=3)
    r = (await api.get("/dashboard/summary", params={"date": day.isoformat()}, headers=bearer(demo_token))).json()
    lo = datetime.combine(day, time(0), tzinfo=IST)
    hi = lo + timedelta(days=1)
    row = db.execute(
        """
        with f as (select payer_id, min(paid_at) fp from transactions where merchant_id = %(m)s and payer_id is not null
                   group by payer_id)
        select coalesce(sum(t.amount_paise),0) c, count(*) n,
               count(distinct t.payer_id) filter (where f.fp >= %(lo)s) np,
               coalesce(sum(t.amount_paise) filter (where f.fp < %(lo)s),0) rp
          from transactions t left join f on f.payer_id = t.payer_id
         where t.merchant_id = %(m)s and t.paid_at >= %(lo)s and t.paid_at < %(hi)s
        """, {"m": mid, "lo": lo, "hi": hi}).fetchone()
    k = r["kpis"]
    assert r["is_today"] is False and r["slow_day"] is None
    assert (k["collected_paise"], k["payments"], k["new_payers"], k["repeat_paise"]) == (row["c"], row["n"], row["np"], row["rp"])
    prev = db.execute("select coalesce(sum(amount_paise),0) s from transactions where merchant_id = %s "
                      "and paid_at >= %s and paid_at < %s", (mid, lo - timedelta(days=7), hi - timedelta(days=7))).fetchone()
    assert k["compare_collected_paise"] == prev["s"] and k["compare_day"] == (day - timedelta(days=7)).isoformat()
    assert all(lo <= datetime.fromisoformat(t["paid_at"]) < hi for t in r["recent"]) and len(r["recent"]) <= 8
    assert [p["bucket"] for p in r["trend"]][-1] == day.isoformat() and len(r["trend"]) == 7


async def test_slow_day_matches_spec_sql(api, demo_token, db, mid):
    """'today up to this hour is at least 30% below the same weekday's 4-week average to the same hour'."""
    r = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    now = datetime.fromisoformat(r["now"])
    today = now.astimezone(IST).date()
    tots = []
    for k in range(5):
        lo = datetime.combine(today - timedelta(days=7 * k), time(0), tzinfo=IST)
        tots.append(int(db.execute("select coalesce(sum(amount_paise),0) s from transactions where merchant_id = %s "
                                   "and paid_at >= %s and paid_at <= %s", (mid, lo, now - timedelta(days=7 * k))).fetchone()["s"]))
    base = sum(tots[1:]) / 4
    sd = r["slow_day"]
    assert sd["today_paise"] == tots[0]
    assert abs(sd["baseline_paise"] - base) <= 100
    if base >= 500_00:
        assert sd["is_slow"] == (tots[0] <= 0.7 * base), (tots, sd)


async def test_lost_regulars_match_spec_rule_with_exact_median(api, demo_token, db, mid):
    """'now - last_paid_at > 3 x median gap and at least 10 payments' with an unrounded median."""
    r = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    rows = db.execute(
        """
        with g as (select payer_id, paid_at,
                          extract(epoch from paid_at - lag(paid_at) over (partition by payer_id order by paid_at)) / 86400.0 gap
                     from transactions where merchant_id = %(m)s and payer_id is not null)
        select p.display_name from g join payers p on p.id = g.payer_id where p.merged_into is null
         group by p.display_name
        having count(*) >= 10
           and extract(epoch from app_now() - max(paid_at)) / 86400.0 > 3 * percentile_cont(0.5) within group (order by gap)
        """, {"m": mid}).fetchall()
    assert sorted(p["display_name"] for p in r["lost_regulars"]) == sorted(x["display_name"] for x in rows) == ["Meena Kapoor"]
    meena = r["lost_regulars"][0]
    assert meena["days_since_last"] == 12 and "Lapsed" in meena["badges"]


async def test_heatmap_and_peaks_match_last_28_days(api, demo_token, db, mid):
    r = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    rows = db.execute(
        """select extract(hour from paid_at at time zone 'Asia/Kolkata')::int h, sum(amount_paise) s, count(*) n
             from transactions where merchant_id = %s and paid_at > app_now() - interval '28 days' and paid_at <= app_now()
            group by 1 order by 2 desc""", (mid,)).fetchall()
    assert sum(c["total_paise"] for c in r["heatmap"]) == sum(x["s"] for x in rows)
    assert sum(c["txn_count"] for c in r["heatmap"]) == sum(x["n"] for x in rows)
    assert [p["hour"] for p in r["peak_hours"]] == [x["h"] for x in rows[:3]]


@pytest.mark.parametrize("days,grain", [(30, "day"), (91, "week"), (180, "month")])
async def test_trend_matches_sql(api, demo_token, db, mid, days, grain):
    r = (await api.get("/dashboard/trend", params={"days": days, "grain": grain}, headers=bearer(demo_token))).json()
    pts = r["points"]
    end_day = date.fromisoformat(r["end"])
    assert end_day == app_now(db).astimezone(IST).date()
    first = date.fromisoformat(pts[0]["bucket"])
    lo = datetime.combine(first, time(0), tzinfo=IST)
    hi = datetime.combine(end_day + timedelta(days=1), time(0), tzinfo=IST)
    s = db.execute("select coalesce(sum(amount_paise),0) s, count(*) n from transactions where merchant_id = %s "
                   "and paid_at >= %s and paid_at < %s", (mid, lo, hi)).fetchone()
    assert sum(p["total_paise"] for p in pts) == s["s"] and sum(p["txn_count"] for p in pts) == s["n"]
    if grain == "day":
        assert len(pts) == days
    if grain == "week":
        assert all(date.fromisoformat(p["bucket"]).weekday() == 0 for p in pts)


# ---------------------------------------------------------------------------
# Payer profile
# ---------------------------------------------------------------------------
async def test_profile_monthly_and_transactions_match_sql(api, demo_token, db, mid):
    ram = db.execute("select id from payers where merchant_id = %s and display_name = 'Ram'", (mid,)).fetchone()["id"]
    p = (await api.get(f"/payers/{ram}", headers=bearer(demo_token))).json()
    rows = db.execute(
        """select to_char(date_trunc('month', paid_at at time zone 'Asia/Kolkata'), 'YYYY-MM') m, sum(amount_paise) s, count(*) n
             from transactions where payer_id = %s group by 1""", (ram,)).fetchall()
    by = {x["m"]: (x["s"], x["n"]) for x in rows}
    assert len(p["monthly"]) == 6
    for m in p["monthly"]:
        assert (m["total_paise"], m["txn_count"]) == by.get(m["month"], (0, 0)), m
    paid = [t["paid_at"] for t in p["transactions"]]
    assert paid == sorted(paid, reverse=True) and len(paid) == 121
    assert {(i["kind"], i["value"]) for i in p["identifiers"]} == {("vpa", "ram@paytm"), ("phone", "9876543210")}


# ---------------------------------------------------------------------------
# Edits (throwaway merchant)
# ---------------------------------------------------------------------------
async def test_email_conflict_is_case_insensitive(api, other_merchant, db):
    h = bearer(other_merchant["token"])
    m, b = other_merchant["merchant_id"], other_merchant["payer_b"]
    db.execute("insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s, %s, 'email', 'asha@example.com')",
               (other_merchant["payer_a"], m))
    r = await api.patch(f"/payers/{b}", json={"email": "ASHA@Example.com"}, headers=h)
    assert r.status_code == 409, r.text
    assert "merge" in r.json()["detail"].lower()


async def test_phone_conflict_with_formatted_number(api, other_merchant):
    h = bearer(other_merchant["token"])
    r = await api.patch(f"/payers/{other_merchant['payer_a']}", json={"phone": "+91 90000 00001"}, headers=h)
    assert r.status_code == 409, r.text


async def test_email_edit_replaces_identifier_and_search_follows(api, other_merchant, db):
    h = bearer(other_merchant["token"])
    a, m = other_merchant["payer_a"], other_merchant["merchant_id"]
    db.execute("insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s, %s, 'email', 'asha@example.com')",
               (a, m))
    r = await api.patch(f"/payers/{a}", json={"email": "Asha.New@Example.com"}, headers=h)
    assert r.status_code == 200, r.text
    ids = {(i["kind"], i["value"]) for i in r.json()["identifiers"]}
    assert ("email", "asha.new@example.com") in ids and ("email", "asha@example.com") not in ids
    assert r.json()["email"] == "asha.new@example.com"
    names = [p["display_name"] for p in (await api.get("/payers", params={"q": "asha.new@"}, headers=h)).json()["payers"]]
    assert names and names[0] == "Asha Test", names


async def test_clearing_phone_removes_identifier(api, other_merchant):
    h = bearer(other_merchant["token"])
    b = other_merchant["payer_b"]
    r = await api.patch(f"/payers/{b}", json={"phone": None}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["phone"] is None
    assert ("phone", "9000000001") not in {(i["kind"], i["value"]) for i in r.json()["identifiers"]}
    # The number is free again for someone else.
    assert (await api.patch(f"/payers/{other_merchant['payer_a']}", json={"phone": "9000000001"}, headers=h)).status_code == 200


async def test_patch_rejects_bad_gstin_and_accepts_valid(api, other_merchant):
    h = bearer(other_merchant["token"])
    a = other_merchant["payer_a"]
    for bad in ("27AAPFS1234K1Z", "27AAPFS1234K1Z55", "AB12345678901Z5"):
        assert (await api.patch(f"/payers/{a}", json={"gstin": bad}, headers=h)).status_code == 422, bad
    r = await api.patch(f"/payers/{a}", json={"gstin": "27AAPFS1234K1Z5"}, headers=h)
    assert r.status_code == 200 and r.json()["gstin"] == "27AAPFS1234K1Z5"
    r = await api.patch(f"/payers/{a}", json={"gstin": None}, headers=h)
    assert r.status_code == 200 and r.json()["gstin"] is None


async def test_cross_merchant_writes_are_404(api, demo_token, other_merchant, db, mid):
    ram = db.execute("select id from payers where merchant_id = %s and display_name = 'Ram'", (mid,)).fetchone()["id"]
    txn = db.execute("select id from transactions where payer_id = %s limit 1", (ram,)).fetchone()["id"]
    h = bearer(other_merchant["token"])
    assert (await api.get(f"/payers/{ram}", headers=h)).status_code == 404
    assert (await api.patch(f"/payers/{ram}", json={"notes": "x"}, headers=h)).status_code == 404
    assert (await api.patch(f"/transactions/{txn}", json={"description": "x"}, headers=h)).status_code == 404
    assert (await api.post(f"/payers/{other_merchant['payer_a']}/merge", json={"other_payer_id": str(ram)},
                           headers=h)).status_code == 404
    assert (await api.post(f"/payers/{ram}/merge", json={"other_payer_id": str(other_merchant['payer_a'])},
                           headers=h)).status_code == 404
    names = [p["display_name"] for p in (await api.get("/payers", params={"q": "ram@paytm"}, headers=h)).json()["payers"]]
    assert names == []


# ---------------------------------------------------------------------------
# Merge semantics ("Merging moves identifiers")
# ---------------------------------------------------------------------------
async def test_merge_moves_identifiers_history_and_search(api, other_merchant, db):
    h = bearer(other_merchant["token"])
    a, b = other_merchant["payer_a"], other_merchant["payer_b"]
    before = (await api.get("/transactions", params={"payer_id": str(b)}, headers=h)).json()["txn_count"]
    r = await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(b)}, headers=h)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["txn_count"] == 3 and before == 1
    assert p["phone"] == "9000000001", "the survivor picks up contact fields it lacked"
    assert p["email"] == "asha@example.com", "the survivor keeps its own email"
    assert {"asha@okaxis", "asha.t@ybl", "9000000001"} <= {i["value"] for i in p["identifiers"]}
    assert "from upi" in p["notes"]
    # The duplicate's UPI ID and phone now find the survivor, and the duplicate is gone from lists.
    for q in ("asha.t@ybl", "9000000001", "Asha"):
        found = [x["id"] for x in (await api.get("/payers", params={"q": q}, headers=h)).json()["payers"]]
        assert found == [str(a)], (q, found)
    gone = await api.get(f"/payers/{b}", headers=h)
    assert gone.status_code == 200 and gone.json()["merged_into"] == str(a) and gone.json()["txn_count"] == 0
    assert (await api.get("/transactions", params={"payer_id": str(b)}, headers=h)).json()["txn_count"] == 0
    # Edits and merges against the merged-away payer are refused.
    assert (await api.patch(f"/payers/{b}", json={"notes": "x"}, headers=h)).status_code == 409
    assert (await api.post(f"/payers/{b}/merge", json={"other_payer_id": str(a)}, headers=h)).status_code == 409
    assert (await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(b)}, headers=h)).status_code == 409
    assert (await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(a)}, headers=h)).status_code == 422
    assert (await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(uuid.uuid4())}, headers=h)).status_code == 404


async def test_merge_moves_invoices(api, other_merchant, db):
    h = bearer(other_merchant["token"])
    m, a, b = other_merchant["merchant_id"], other_merchant["payer_a"], other_merchant["payer_b"]
    db.execute("insert into invoices (merchant_id, number, payer_id, amount_paise, due_date, status, paytm_order_id) "
               "values (%s, 'INV-9001', %s, 50000, current_date + 3, 'sent', %s)", (m, b, f"R2_{uuid.uuid4().hex[:10]}"))
    r = await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(b)}, headers=h)
    assert r.status_code == 200
    assert [i["number"] for i in r.json()["invoices"]] == ["INV-9001"]


async def test_phone_in_0091_format_is_the_same_number(api, other_merchant):
    """'One ledger per person': 0091 90000 00001 is payer B's 9000000001 written the international way,
    so giving it to payer A must be refused like '+91 90000 00001' is."""
    h = bearer(other_merchant["token"])
    r = await api.patch(f"/payers/{other_merchant['payer_a']}", json={"phone": "0091 90000 00001"}, headers=h)
    assert r.status_code == 409, (r.status_code, r.json().get("phone") if r.status_code == 200 else r.text)
