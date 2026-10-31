"""Tester M2: ledger API against ARCHITECTURE.md ("API surface", "Decisions/Identity",
"Low-friction ledger") and PLAN.md M2 (JWT auth -> merchant, GET /dashboard/summary,
payers search/profile/patch/merge, PATCH /transactions/{id}, transactions list).

Read-only checks run on the demo merchant; anything that writes uses the throwaway
`other_merchant` fixture (or extra throwaway rows inside it).
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import date, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.money import IST

from ..conftest import bearer, create_user, delete_user, sign_in

ROUTES = [
    ("GET", "/me", None),
    ("GET", "/dashboard/summary", None),
    ("GET", "/dashboard/trend", None),
    ("GET", "/payers", None),
    ("GET", "/payers/{id}", None),
    ("PATCH", "/payers/{id}", {"notes": "x"}),
    ("POST", "/payers/{id}/merge", {"other_payer_id": str(uuid.uuid4())}),
    ("GET", "/transactions", None),
    ("PATCH", "/transactions/{id}", {"description": "x"}),
]


@pytest.fixture
def mid(db, settings, seeded):
    row = db.execute(
        "select m.id from merchants m join auth.users u on u.id = m.owner_user_id where u.email = %s",
        (settings.demo_merchant_email,),
    ).fetchone()
    return row["id"]


async def _call(api, method, path, token=None, body=None, headers=None):
    path = path.replace("{id}", str(uuid.uuid4()))
    h = headers if headers is not None else (bearer(token) if token else {})
    return await api.request(method, path, headers=h, json=body)


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Auth: "Supabase JWT on all routes except webhooks, cron and mock"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_every_route_needs_a_token(api, method, path, body):
    r = await _call(api, method, path, body=body)
    assert r.status_code == 401, r.text


async def test_forged_tokens_are_rejected(api, demo_token):
    claims = jwt.decode(demo_token, options={"verify_signature": False})
    hdr = jwt.get_unverified_header(demo_token)
    attacker = ec.generate_private_key(ec.SECP256R1())
    head, _, sig = demo_token.split(".")
    forged = {
        "alg none": _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(claims) + ".",
        "HS256": jwt.encode(claims, "x" * 32, algorithm="HS256"),
        "ES256 other key, real kid": jwt.encode({**claims, "exp": int(time.time()) + 600}, attacker,
                                                algorithm="ES256", headers={"kid": hdr["kid"]}),
        "ES256 other key, no kid": jwt.encode({**claims, "exp": int(time.time()) + 600}, attacker, algorithm="ES256"),
        "payload swapped": head + "." + _b64({**claims, "sub": str(uuid.uuid4())}) + "." + sig,
        "expired-looking garbage": "a.b.c",
    }
    for name, tok in forged.items():
        r = await api.get("/me", headers=bearer(tok))
        assert r.status_code == 401, (name, r.status_code, r.text)
    assert (await api.get("/me", headers={"Authorization": f"Basic {demo_token}"})).status_code == 401


async def test_user_without_merchant_gets_403_everywhere(api, settings):
    email = f"tester-nom-{uuid.uuid4().hex[:8]}@hisaabbolo.test"
    uid = create_user(settings, email, "pass-12345678")
    try:
        tok = sign_in(settings, email, "pass-12345678")
        for method, path, body in ROUTES:
            r = await _call(api, method, path, tok, body)
            assert r.status_code == 403, (method, path, r.status_code, r.text)
    finally:
        delete_user(settings, uid)


# ---------------------------------------------------------------------------
# Dashboard KPIs and trend vs SQL
# ---------------------------------------------------------------------------
async def test_dashboard_kpis_match_sql(api, demo_token, db, mid):
    d = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    now = datetime.fromisoformat(d["now"])
    today = now.astimezone(IST).date()
    q = """
      with f as (select payer_id, min(paid_at) fp from transactions where merchant_id = %(m)s
                 and payer_id is not null group by payer_id)
      select coalesce(sum(t.amount_paise), 0) s, count(*) n,
             count(distinct t.payer_id) filter (where f.fp >= %(lo)s) newp,
             coalesce(sum(t.amount_paise) filter (where f.fp < %(lo)s), 0) rep
        from transactions t left join f on f.payer_id = t.payer_id
       where t.merchant_id = %(m)s and t.paid_at >= %(lo)s and t.paid_at <= %(hi)s"""
    lo = datetime(today.year, today.month, today.day, tzinfo=IST)
    cur = db.execute(q, {"m": mid, "lo": lo, "hi": now}).fetchone()
    prev = db.execute(q, {"m": mid, "lo": lo - timedelta(days=7), "hi": now - timedelta(days=7)}).fetchone()
    k = d["kpis"]
    assert (k["collected_paise"], k["payments"], k["new_payers"], k["repeat_paise"]) == \
        (cur["s"], cur["n"], cur["newp"], cur["rep"])
    assert k["repeat_share"] == pytest.approx(float(cur["rep"]) / float(cur["s"]))
    assert k["compare_collected_paise"] == prev["s"]
    assert k["change_vs_last_week"] == pytest.approx(float(cur["s"]) / float(prev["s"]) - 1)
    assert k["compare_day"] == (today - timedelta(days=7)).isoformat()


async def test_dashboard_money_fields_are_integer_paise(api, demo_token):
    d = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    bad = []

    def walk(o, path=""):
        if isinstance(o, dict):
            for key, v in o.items():
                if key.endswith("_paise") and v is not None and not isinstance(v, int):
                    bad.append((path + key, v))
                walk(v, path + key + ".")
        elif isinstance(o, list):
            for v in o:
                walk(v, path)
    walk(d)
    assert not bad, bad


async def test_dashboard_trend_heatmap_peaks_match_views(api, demo_token, db, mid):
    d = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    today = date.fromisoformat(d["date"])
    daily = {r["day"]: r for r in db.execute(
        "select day, total_paise, txn_count from daily_totals where merchant_id = %s", (mid,)).fetchall()}
    want = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    assert [p["bucket"] for p in d["trend"]] == [x.isoformat() for x in want]
    assert [p["total_paise"] for p in d["trend"]] == [daily[x]["total_paise"] if x in daily else 0 for x in want]
    hm = db.execute("select dow, hour, total_paise, txn_count from hourly_heatmap where merchant_id = %s "
                    "order by dow, hour", (mid,)).fetchall()
    assert d["heatmap"] == [dict(r) for r in hm]
    by_hour = {}
    for c in hm:
        by_hour[c["hour"]] = by_hour.get(c["hour"], 0) + c["total_paise"]
    top = sorted(by_hour.items(), key=lambda kv: kv[1], reverse=True)[:3]
    assert [(p["hour"], p["total_paise"]) for p in d["peak_hours"]] == top


async def test_dashboard_lost_regulars_match_spec_rule(api, demo_token, db, mid):
    """Lost-regular alert: now - last_paid_at > 3 x median gap and at least 10 payments."""
    d = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    rows = db.execute(
        "select p.display_name from payer_stats s join payers p on p.id = s.payer_id "
        "where s.merchant_id = %s and p.merged_into is null and s.txn_count >= 10 "
        "and app_now() - s.last_paid_at > 3 * s.median_gap_days * interval '1 day'", (mid,)).fetchall()
    assert sorted(p["display_name"] for p in d["lost_regulars"]) == sorted(r["display_name"] for r in rows)


async def test_dashboard_slow_day_matches_spec_rule(api, demo_token, db, mid):
    """Slow-day banner: today so far at least 30% below the same weekday's 4-week average to the same time."""
    d = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    rows = {r["k"]: r["p"] for r in db.execute(
        "select k, coalesce(sum(t.amount_paise), 0)::bigint p from generate_series(0, 4) k "
        "left join transactions t on t.merchant_id = %(m)s "
        " and t.paid_at >= ((app_today() - 7 * k)::timestamp at time zone 'Asia/Kolkata') "
        " and t.paid_at <= app_now() - 7 * k * interval '1 day' group by k", {"m": mid}).fetchall()}
    base = sum(rows[k] for k in range(1, 5)) / 4
    sd = d["slow_day"]
    assert sd["today_paise"] == rows[0]
    assert abs(sd["baseline_paise"] - base) <= 50
    assert sd["is_slow"] == (base > 0 and rows[0] <= 0.7 * base and base >= 500_00)


async def test_dashboard_recent_and_outstanding(api, demo_token, db, mid):
    d = (await api.get("/dashboard/summary", headers=bearer(demo_token))).json()
    latest = db.execute(
        "select id from transactions where merchant_id = %s and paid_at >= "
        "(app_today()::timestamp at time zone 'Asia/Kolkata') and paid_at <= app_now() "
        "order by paid_at desc limit 8", (mid,)).fetchall()
    assert [t["id"] for t in d["recent"]] == [str(r["id"]) for r in latest]
    inv = db.execute("select number, amount_paise from invoices where merchant_id = %s and status in ('sent','overdue')",
                     (mid,)).fetchall()
    assert sorted(i["number"] for i in d["outstanding"]["invoices"]) == sorted(r["number"] for r in inv)
    assert d["outstanding"]["total_paise"] == sum(r["amount_paise"] for r in inv)


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01", "30-10-2026", ""])
async def test_dashboard_bad_date_is_422(api, demo_token, bad):
    r = await api.get("/dashboard/summary", params={"date": bad}, headers=bearer(demo_token))
    assert r.status_code in (200, 422) if bad == "" else r.status_code == 422, r.text


async def test_dashboard_future_date_is_empty_not_error(api, demo_token):
    r = await api.get("/dashboard/summary", params={"date": "2031-01-01"}, headers=bearer(demo_token))
    assert r.status_code == 200
    d = r.json()
    assert d["kpis"]["collected_paise"] == 0 and d["recent"] == [] and d["slow_day"] is None


@pytest.mark.parametrize("params", [{"days": 0}, {"days": 367}, {"grain": "year"}, {"end": "nope"}])
async def test_trend_validation(api, demo_token, params):
    r = await api.get("/dashboard/trend", params=params, headers=bearer(demo_token))
    assert r.status_code == 422


@pytest.mark.parametrize("days,grain", [(30, "day"), (91, "week"), (90, "month")])
async def test_trend_sums_match_daily_totals(api, demo_token, db, mid, days, grain):
    body = (await api.get("/dashboard/trend", params={"days": days, "grain": grain}, headers=bearer(demo_token))).json()
    end = date.fromisoformat(body["end"])
    # Week/month windows start on a whole bucket (see test_trend_weekly_buckets_are_whole_weeks).
    from app.services.ledger import trend_start

    start = trend_start(end, days, grain)
    total = db.execute("select coalesce(sum(total_paise), 0) s from daily_totals where merchant_id = %s "
                       "and day between %s and %s", (mid, start, end)).fetchone()["s"]
    assert sum(p["total_paise"] for p in body["points"]) == total


async def test_trend_weekly_buckets_are_whole_weeks(api, demo_token, db, mid):
    """The 90D chart labels each point 'w/c <Monday>' with that week's total. A bucket that starts
    before the window silently drops the days outside it, so the first week reads as a dip."""
    body = (await api.get("/dashboard/trend", params={"days": 91, "grain": "week"}, headers=bearer(demo_token))).json()
    end = date.fromisoformat(body["end"])
    start = end - timedelta(days=90)
    first = body["points"][0]
    monday = date.fromisoformat(first["bucket"])
    assert monday >= start, (
        f"first bucket w/c {monday} only covers {start}..{monday + timedelta(days=6)} "
        f"({(monday + timedelta(days=7) - start).days} of 7 days): {first}")


# ---------------------------------------------------------------------------
# Search: trigram names, UPI ID, phone
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("q,top", [
    ("ram", "Ram"), ("raam", "Ram"), ("RAM", "Ram"), ("  ram  ", "Ram"), ("ram@paytm", "Ram"), ("ram@", "Ram"),
    ("9876543210", "Ram"), ("98765", "Ram"), ("meena kapur", "Meena Kapoor"), ("sharma tradres", "Sharma Traders"),
    ("accounts@sharmatraders.in", "Sharma Traders"), ("priya", "Priya Caterers"), ("anita.desai@okaxis", "Anita Desai"),
    ("vikrm", None),
])
async def test_search_quality(api, demo_token, q, top):
    r = await api.get("/payers", params={"q": q}, headers=bearer(demo_token))
    assert r.status_code == 200
    names = [p["display_name"] for p in r.json()["payers"]]
    if top:
        assert names and names[0] == top, names
    else:
        assert names and names[0].startswith("Vikram"), names


@pytest.mark.parametrize("q", ["+91 98765 43210", "+919876543210", "98765 43210", "98765-43210"])
async def test_search_phone_in_common_indian_formats(api, demo_token, q):
    """UI placeholder: 'Search by name, UPI ID or phone'. Ram's phone is 9876543210."""
    names = [p["display_name"] for p in (await api.get("/payers", params={"q": q}, headers=bearer(demo_token))).json()["payers"]]
    assert names[:1] == ["Ram"], f"{q!r} -> {names}"


@pytest.mark.parametrize("q", ["%", "_", "%%"])
async def test_search_treats_like_wildcards_literally(api, demo_token, q):
    names = [p["display_name"] for p in (await api.get("/payers", params={"q": q, "limit": 200},
                                                       headers=bearer(demo_token))).json()["payers"]]
    assert names == [], f"q={q!r} matched {len(names)} payers"


@pytest.mark.parametrize("limit", [0, 201, -1, "x"])
async def test_search_limit_validation(api, demo_token, limit):
    assert (await api.get("/payers", params={"limit": limit}, headers=bearer(demo_token))).status_code == 422


async def test_payer_list_shape(api, demo_token):
    payers = (await api.get("/payers", params={"limit": 200}, headers=bearer(demo_token))).json()["payers"]
    assert len(payers) == 40
    need = {"id", "display_name", "vpa", "txn_count", "total_paise", "habit", "badges", "lost", "last_paid_at",
            "median_gap_days", "typical_time", "days_since_last"}
    for p in payers:
        assert need <= set(p), need - set(p)
        assert p["merged_into"] is None


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
async def test_profile_validation_and_isolation(api, demo_token, other_merchant):
    h = bearer(demo_token)
    assert (await api.get("/payers/not-a-uuid", headers=h)).status_code == 422
    assert (await api.get(f"/payers/{other_merchant['payer_a']}", headers=h)).status_code == 404
    assert (await api.patch(f"/payers/{other_merchant['payer_a']}", json={"notes": "x"}, headers=h)).status_code == 404
    r = await api.post(f"/payers/{other_merchant['payer_a']}/merge",
                       json={"other_payer_id": str(other_merchant["payer_b"])}, headers=h)
    assert r.status_code == 404
    rows = (await api.get("/transactions", params={"payer_id": str(other_merchant["payer_a"])}, headers=h)).json()
    assert rows["transactions"] == [] and rows["txn_count"] == 0


async def test_cannot_merge_another_merchants_payer_into_mine(api, demo_token, other_merchant, db):
    h = bearer(other_merchant["token"])
    ram = (await api.get("/payers", params={"q": "ram"}, headers=bearer(demo_token))).json()["payers"][0]["id"]
    r = await api.post(f"/payers/{other_merchant['payer_a']}/merge", json={"other_payer_id": ram}, headers=h)
    assert r.status_code == 404
    assert db.execute("select merged_into from payers where id = %s", (ram,)).fetchone()["merged_into"] is None


async def test_profile_numbers_match_sql(api, demo_token, db, mid):
    sharma = (await api.get("/payers", params={"q": "sharma traders"}, headers=bearer(demo_token))).json()["payers"][0]
    p = (await api.get(f"/payers/{sharma['id']}", headers=bearer(demo_token))).json()
    s = db.execute("select count(*) n, sum(amount_paise) s from transactions where payer_id = %s", (sharma["id"],)).fetchone()
    assert (p["txn_count"], p["total_paise"]) == (s["n"], s["s"])
    assert len(p["transactions"]) == s["n"]
    assert all(t["invoice_number"] for t in p["transactions"])
    assert {i["number"] for i in p["invoices"]} >= {"INV-0412"}
    assert any(i["kind"] == "vpa" for i in p["identifiers"])


# ---------------------------------------------------------------------------
# PATCH /payers/{id}
# ---------------------------------------------------------------------------
async def test_patch_payer_notes_null_is_not_a_500(api, other_merchant):
    r = await api.patch(f"/payers/{other_merchant['payer_a']}", json={"notes": None},
                        headers=bearer(other_merchant["token"]))
    assert r.status_code in (200, 422), f"{r.status_code} {r.text[:200]}"


async def test_patch_payer_blank_phone_rejected(api, other_merchant, db):
    pid = other_merchant["payer_a"]
    r = await api.patch(f"/payers/{pid}", json={"phone": "       "}, headers=bearer(other_merchant["token"]))
    blanks = db.execute("select kind, value from payer_identifiers where payer_id = %s and trim(value) = ''",
                        (pid,)).fetchall()
    assert r.status_code == 422 and not blanks, (r.status_code, r.json().get("phone"), blanks)


async def test_patch_payer_trims_display_name(api, other_merchant):
    r = await api.patch(f"/payers/{other_merchant['payer_a']}", json={"display_name": "  Asha Renamed  "},
                        headers=bearer(other_merchant["token"]))
    assert r.status_code == 200
    assert r.json()["display_name"] == "Asha Renamed"


async def test_patch_payer_ignores_foreign_fields(api, other_merchant, db, mid):
    pid = other_merchant["payer_a"]
    r = await api.patch(f"/payers/{pid}", json={"merchant_id": str(mid), "merged_into": str(other_merchant["payer_b"]),
                                               "notes": "ok"}, headers=bearer(other_merchant["token"]))
    assert r.status_code == 200
    row = db.execute("select merchant_id, merged_into from payers where id = %s", (pid,)).fetchone()
    assert row["merchant_id"] == other_merchant["merchant_id"] and row["merged_into"] is None


@pytest.mark.parametrize("body", [{"display_name": "x" * 121}, {"email": "not-an-email"}, {"phone": "abc"},
                                  {"gstin": "27abcde1234f1z5"}, {"notes": "n" * 2001}, {"display_name": ""}])
async def test_patch_payer_validation(api, other_merchant, body):
    r = await api.patch(f"/payers/{other_merchant['payer_a']}", json=body, headers=bearer(other_merchant["token"]))
    assert r.status_code == 422, (body, r.status_code)


async def test_patch_payer_phone_owned_by_someone_else(api, other_merchant, db):
    """Identity: one identifier maps to one payer. Giving payer A the phone that already identifies
    payer B must not leave A's card showing a phone that payments would credit to B."""
    a, b = other_merchant["payer_a"], other_merchant["payer_b"]
    r = await api.patch(f"/payers/{a}", json={"phone": "9000000001"}, headers=bearer(other_merchant["token"]))
    owner = db.execute("select payer_id from payer_identifiers where merchant_id = %s and kind = 'phone' "
                       "and value = '9000000001'", (other_merchant["merchant_id"],)).fetchone()["payer_id"]
    if r.status_code == 200:
        assert owner == a or r.json()["phone"] != "9000000001", (
            "200 OK: A.phone = 9000000001 but the phone identifier still belongs to B")
    else:
        assert r.status_code == 409


async def test_patch_payer_formatted_phone_is_searchable(api, other_merchant):
    h = bearer(other_merchant["token"])
    pid = other_merchant["payer_a"]
    assert (await api.patch(f"/payers/{pid}", json={"phone": "+91 98200 55555"}, headers=h)).status_code == 200
    names = [p["display_name"] for p in (await api.get("/payers", params={"q": "9820055555"}, headers=h)).json()["payers"]]
    assert names == ["Asha Test"], names


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
async def test_merge_chain_and_schedules(api, other_merchant, db):
    h = bearer(other_merchant["token"])
    m, a, b = other_merchant["merchant_id"], other_merchant["payer_a"], other_merchant["payer_b"]
    c = db.execute("insert into payers (merchant_id, display_name, gstin) values (%s, 'Asha Corp', '27AAPFS1234K1Z5') "
                   "returning id", (m,)).fetchone()["id"]
    db.execute("insert into recurring_schedules (merchant_id, payer_id, amount_paise, cadence, anchor_date, next_due_date) "
               "values (%s, %s, 50000, 'weekly', current_date, current_date + 7), "
               "(%s, %s, 70000, 'monthly', current_date, current_date + 30)", (m, a, m, c))
    assert (await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(b)}, headers=h)).status_code == 200
    r = await api.post(f"/payers/{c}/merge", json={"other_payer_id": str(a)}, headers=h)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["txn_count"] == 3 and p["gstin"] == "27AAPFS1234K1Z5" and p["email"] == "asha@example.com"
    assert {i["value"] for i in p["identifiers"]} >= {"asha@okaxis", "asha.t@ybl", "9000000001"}
    pts = {r["id"]: r["merged_into"] for r in db.execute("select id, merged_into from payers where merchant_id = %s",
                                                          (m,)).fetchall()}
    assert pts[a] == c and pts[b] == c, "earlier merges must point at the final survivor"
    active = db.execute("select payer_id, amount_paise from recurring_schedules where merchant_id = %s and active",
                        (m,)).fetchall()
    assert [(r["payer_id"], r["amount_paise"]) for r in active] == [(c, 70000)]
    # The merged payer still resolves (UI redirects) but can't be edited or merged again.
    gone = (await api.get(f"/payers/{a}", headers=h)).json()
    assert gone["merged_into"] == str(c)
    assert (await api.post(f"/payers/{c}/merge", json={"other_payer_id": str(a)}, headers=h)).status_code == 409
    assert (await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(c)}, headers=h)).status_code == 409


@pytest.mark.parametrize("body", [{}, {"other_payer_id": "nope"}, {"other_payer_id": None}])
async def test_merge_body_validation(api, other_merchant, body):
    r = await api.post(f"/payers/{other_merchant['payer_a']}/merge", json=body, headers=bearer(other_merchant["token"]))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("body", [{"description": "   "}, {"description": "x" * 121}, {"category": ""},
                                  {"category": "c" * 41}, {"description": None}])
async def test_patch_transaction_validation(api, other_merchant, body):
    r = await api.patch(f"/transactions/{other_merchant['txns'][0]}", json=body, headers=bearer(other_merchant["token"]))
    assert r.status_code == 422, (body, r.status_code, r.text[:200])


async def test_patch_transaction_merchant_edit_wins(api, other_merchant, db):
    tid = other_merchant["txns"][0]
    db.execute("update transactions set description = 'AI guess', description_by = 'ai' where id = %s", (tid,))
    r = await api.patch(f"/transactions/{tid}", json={"description": "  Diwali sweets  "},
                        headers=bearer(other_merchant["token"]))
    assert r.status_code == 200
    t = r.json()
    assert t["description_by"] == "merchant" and t["description"].strip() == "Diwali sweets"
    assert set(t) >= {"id", "payer_id", "payer_name", "amount_paise", "paid_at", "description", "category", "invoice_number"}


async def test_transactions_list_paging_and_filters(api, demo_token, db, mid):
    h = bearer(demo_token)
    all_ = (await api.get("/transactions", params={"limit": 500}, headers=h)).json()
    n = db.execute("select count(*) n, sum(amount_paise) s from transactions where merchant_id = %s", (mid,)).fetchone()
    assert (all_["txn_count"], all_["total_paise"]) == (n["n"], n["s"])
    p1 = (await api.get("/transactions", params={"limit": 5, "offset": 0}, headers=h)).json()["transactions"]
    p2 = (await api.get("/transactions", params={"limit": 5, "offset": 5}, headers=h)).json()["transactions"]
    assert [t["id"] for t in p1 + p2] == [t["id"] for t in all_["transactions"][:10]]
    frm = (datetime.now(IST) - timedelta(days=3)).isoformat()
    to = (datetime.now(IST) - timedelta(days=1)).isoformat()
    win = (await api.get("/transactions", params={"from": frm, "to": to, "limit": 500}, headers=h)).json()
    assert all(frm <= t["paid_at"] or datetime.fromisoformat(t["paid_at"]) >= datetime.fromisoformat(frm)
               for t in win["transactions"])
    assert all(datetime.fromisoformat(t["paid_at"]) < datetime.fromisoformat(to) for t in win["transactions"])
    for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}, {"from": "x"}, {"payer_id": "x"}):
        assert (await api.get("/transactions", params=bad, headers=h)).status_code == 422, bad
