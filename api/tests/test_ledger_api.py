"""M2: ledger API (auth, dashboard, payers, transactions)."""

import uuid
from datetime import date, timedelta

import pytest

from .conftest import bearer, create_user, delete_user, sign_in


async def test_requires_token(api):
    r = await api.get("/dashboard/summary")
    assert r.status_code == 401


async def test_rejects_garbage_token(api):
    r = await api.get("/me", headers=bearer("not-a-jwt"))
    assert r.status_code == 401


async def test_rejects_hs256_token(api):
    import jwt

    forged = jwt.encode({"sub": str(uuid.uuid4()), "aud": "authenticated"}, "x" * 32, algorithm="HS256")
    r = await api.get("/me", headers=bearer(forged))
    assert r.status_code == 401


async def test_user_without_merchant_is_forbidden(api, settings):
    email = f"nomerchant-{uuid.uuid4().hex[:8]}@hisaabbolo.test"
    uid = create_user(settings, email, "pass-12345678")
    try:
        r = await api.get("/me", headers=bearer(sign_in(settings, email, "pass-12345678")))
        assert r.status_code == 403
    finally:
        delete_user(settings, uid)


async def test_me(api, demo_token):
    r = await api.get("/me", headers=bearer(demo_token))
    assert r.status_code == 200
    body = r.json()
    assert body["merchant"]["name"] == "Gupta Kirana Store"
    assert body["paytm_mode"] == "mock"


async def test_dashboard_summary_today(api, demo_token, db, seeded):
    r = await api.get("/dashboard/summary", headers=bearer(demo_token))
    assert r.status_code == 200
    d = r.json()
    assert d["is_today"]
    expected = db.execute(
        "select coalesce(sum(amount_paise), 0) as s, count(*) as n from transactions where merchant_id = %s "
        "and paid_at >= (app_today()::timestamp at time zone 'Asia/Kolkata') and paid_at <= app_now()",
        (seeded["merchant_id"],),
    ).fetchone()
    assert d["kpis"]["collected_paise"] == expected["s"]
    assert d["kpis"]["payments"] == expected["n"]
    assert 0 <= d["kpis"]["repeat_share"] <= 1
    new_today = db.execute(
        "select count(*) as n from (select payer_id, min(paid_at) as f from transactions where merchant_id = %s "
        "and payer_id is not null group by payer_id) x "
        "where f >= (app_today()::timestamp at time zone 'Asia/Kolkata') and f <= app_now()",
        (seeded["merchant_id"],),
    ).fetchone()["n"]
    assert d["kpis"]["new_payers"] == new_today  # newcomers debut today only after 10:00
    assert len(d["trend"]) == 7 and d["trend"][-1]["bucket"] == d["date"]
    assert d["slow_day"]["is_slow"] and d["slow_day"]["drop_pct"] >= 30
    assert [p["display_name"] for p in d["lost_regulars"]] == ["Meena Kapoor"]
    assert d["outstanding"]["total_paise"] == 11_700_00 and d["outstanding"]["customers"] == 2
    overdue = next(i for i in d["outstanding"]["invoices"] if i["number"] == "INV-0412")
    assert overdue["days_overdue"] == 9
    assert len(d["peak_hours"]) == 3
    assert all(1 <= c["dow"] <= 7 and 0 <= c["hour"] <= 23 for c in d["heatmap"])
    assert len(d["recent"]) <= 8


async def test_dashboard_summary_past_day(api, demo_token):
    day = (date.today() - timedelta(days=10)).isoformat()
    r = await api.get("/dashboard/summary", params={"date": day}, headers=bearer(demo_token))
    assert r.status_code == 200
    d = r.json()
    assert not d["is_today"] and d["slow_day"] is None
    assert d["kpis"]["payments"] > 0
    assert d["kpis"]["compare_day"] == (date.fromisoformat(day) - timedelta(days=7)).isoformat()


@pytest.mark.parametrize("grain,days,count", [("day", 30, 30), ("week", 28, None), ("month", 90, None)])
async def test_trend(api, demo_token, grain, days, count):
    r = await api.get("/dashboard/trend", params={"days": days, "grain": grain}, headers=bearer(demo_token))
    assert r.status_code == 200
    pts = r.json()["points"]
    if count:
        assert len(pts) == count
    assert sum(p["total_paise"] for p in pts) > 0
    assert [p["bucket"] for p in pts] == sorted(p["bucket"] for p in pts)


@pytest.mark.parametrize("q", ["ram", "Ram", "raam", "ram@paytm", "9876543210"])
async def test_search_finds_ram(api, demo_token, q):
    r = await api.get("/payers", params={"q": q}, headers=bearer(demo_token))
    assert r.status_code == 200
    payers = r.json()["payers"]
    assert payers and payers[0]["display_name"] == "Ram", [p["display_name"] for p in payers]
    ram = payers[0]
    assert ram["habit"].startswith("₹100 daily, ~8:30 AM · 121 payments since")
    assert "Regular" in ram["badges"] and ram["vpa"] == "ram@paytm"


async def test_list_all_payers(api, demo_token):
    r = await api.get("/payers", headers=bearer(demo_token))
    payers = r.json()["payers"]
    assert len(payers) == 40
    meena = next(p for p in payers if p["display_name"] == "Meena Kapoor")
    assert meena["lost"] and "Lapsed" in meena["badges"]


async def test_payer_profile(api, demo_token):
    ram_id = (await api.get("/payers", params={"q": "ram"}, headers=bearer(demo_token))).json()["payers"][0]["id"]
    r = await api.get(f"/payers/{ram_id}", headers=bearer(demo_token))
    assert r.status_code == 200
    p = r.json()
    assert p["txn_count"] == 121 and p["total_paise"] == 12_100_00
    assert {"kind": "vpa", "value": "ram@paytm"}.items() <= p["identifiers"][0].items() or any(
        i["kind"] == "vpa" and i["value"] == "ram@paytm" for i in p["identifiers"])
    assert len(p["monthly"]) == 6 and sum(m["txn_count"] for m in p["monthly"]) == 121
    assert len(p["transactions"]) == 121
    assert p["typical_time"] == "8:30 AM"


async def test_sharma_profile_has_invoices(api, demo_token):
    sid = (await api.get("/payers", params={"q": "sharma"}, headers=bearer(demo_token))).json()["payers"][0]["id"]
    p = (await api.get(f"/payers/{sid}", headers=bearer(demo_token))).json()
    assert p["gstin"] and any(i["number"] == "INV-0412" and i["status"] == "overdue" for i in p["invoices"])


async def test_unknown_payer_404(api, demo_token):
    r = await api.get(f"/payers/{uuid.uuid4()}", headers=bearer(demo_token))
    assert r.status_code == 404


async def test_cross_merchant_isolation(api, demo_token, other_merchant):
    # The other merchant cannot read the demo merchant's payer, and vice versa.
    ram_id = (await api.get("/payers", params={"q": "ram"}, headers=bearer(demo_token))).json()["payers"][0]["id"]
    assert (await api.get(f"/payers/{ram_id}", headers=bearer(other_merchant["token"]))).status_code == 404
    assert (await api.get(f"/payers/{other_merchant['payer_a']}", headers=bearer(demo_token))).status_code == 404
    listed = (await api.get("/payers", headers=bearer(other_merchant["token"]))).json()["payers"]
    assert {p["display_name"] for p in listed} == {"Asha Test", "Asha T (UPI)"}
    r = await api.patch(f"/transactions/{other_merchant['txns'][0]}", json={"description": "hijack"},
                        headers=bearer(demo_token))
    assert r.status_code == 404


async def test_patch_payer(api, other_merchant):
    h = bearer(other_merchant["token"])
    pid = other_merchant["payer_a"]
    r = await api.patch(f"/payers/{pid}", json={"email": "Asha.New@Example.com", "phone": "9123456780",
                                               "gstin": "27ABCDE1234F1Z5", "notes": "prefers evening"}, headers=h)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["email"] == "asha.new@example.com" and p["gstin"] == "27ABCDE1234F1Z5"
    kinds = {(i["kind"], i["value"]) for i in p["identifiers"]}
    assert ("email", "asha.new@example.com") in kinds and ("phone", "9123456780") in kinds
    assert (await api.patch(f"/payers/{pid}", json={"gstin": "bad"}, headers=h)).status_code == 422
    assert (await api.patch(f"/payers/{pid}", json={"display_name": "  "}, headers=h)).status_code == 422


async def test_merge_payers(api, other_merchant, db):
    h = bearer(other_merchant["token"])
    a, b = other_merchant["payer_a"], other_merchant["payer_b"]
    r = await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(b)}, headers=h)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["txn_count"] == 3
    values = {i["value"] for i in p["identifiers"]}
    assert {"asha@okaxis", "asha.t@ybl", "9000000001"} <= values
    assert p["phone"] == "9000000001" and "from upi" in p["notes"]
    merged = db.execute("select merged_into from payers where id = %s", (b,)).fetchone()
    assert merged["merged_into"] == a
    listed = (await api.get("/payers", headers=h)).json()["payers"]
    assert [x["display_name"] for x in listed] == ["Asha Test"]
    assert (await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(b)}, headers=h)).status_code == 409
    assert (await api.post(f"/payers/{a}/merge", json={"other_payer_id": str(a)}, headers=h)).status_code == 422
    assert (await api.patch(f"/payers/{b}", json={"notes": "x"}, headers=h)).status_code == 409


async def test_patch_transaction_description(api, other_merchant):
    h = bearer(other_merchant["token"])
    tid = other_merchant["txns"][0]
    r = await api.patch(f"/transactions/{tid}", json={"description": "Diwali sweets", "category": "Festive"}, headers=h)
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["description"] == "Diwali sweets" and t["description_by"] == "merchant" and t["category"] == "Festive"
    assert (await api.patch(f"/transactions/{tid}", json={}, headers=h)).status_code == 422
    assert (await api.patch(f"/transactions/{uuid.uuid4()}", json={"description": "x"}, headers=h)).status_code == 404


async def test_list_transactions_filters(api, demo_token):
    ram_id = (await api.get("/payers", params={"q": "ram"}, headers=bearer(demo_token))).json()["payers"][0]["id"]
    r = await api.get("/transactions", params={"payer_id": ram_id, "limit": 10}, headers=bearer(demo_token))
    body = r.json()
    assert r.status_code == 200 and body["txn_count"] == 121 and body["total_paise"] == 12_100_00
    assert len(body["transactions"]) == 10
    times = [t["paid_at"] for t in body["transactions"]]
    assert times == sorted(times, reverse=True)
