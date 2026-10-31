"""Tester round 4, M4 regression after M5: invoice → paid with the clock and jobs in the loop,
webhook idempotency under a real race, the dashboard's Pending payments (outstanding()), reminders
and the app clock as the source of "today".

Spec: ARCHITECTURE.md moment 2, Collections flow, Risks ("Check payment" + cron polling),
docs/paytm/PAYTM_INTEGRATION.md (handler order, idempotency on TXNID, exact ORDERID match).
Writes use the throwaway `other_merchant`; the demo merchant is re-seeded after the module.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta

import pytest

from app.money import IST
from app.services import email as email_svc
from app.services.paytm.checksum import sign_form

from ..conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def _reseed_after_module(reseed_demo):
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def _fake_descriptions(monkeypatch):
    async def fake_generate(t):
        return {"description": "Invoice payment", "category": "Bulk order"}

    monkeypatch.setattr("app.services.descriptions.generate", fake_generate)


def today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


async def new_invoice(api, m, db, *, due_in=5, payer="payer_a", email="asha@example.com", amount=1500_00, **extra):
    body = {"payer_id": str(m[payer]), "due_date": (today(db) + timedelta(days=due_in)).isoformat(),
            "line_items": [{"name": "Monthly ration", "qty": 1, "unit_price_paise": amount}], "email": email} | extra
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    return r.json()


def signed_fields(settings, inv, txn_id=None, amount=None, **extra) -> dict:
    now = datetime.now(IST)
    f = {
        "ORDERID": inv["paytm_order_id"], "MID": settings.paytm_mid_active,
        "TXNID": txn_id or ("R4M4" + uuid.uuid4().hex[:24].upper()),
        "TXNAMOUNT": amount or f"{inv['amount_paise'] / 100:.2f}", "CURRENCY": "INR", "PAYMENTMODE": "UPI",
        "TXNDATE": now.strftime("%Y-%m-%d"), "TXNDATETIME": now.strftime("%Y-%m-%d %H:%M:%S.0"),
        "STATUS": "TXN_SUCCESS", "RESPCODE": "01", "RESPMSG": "Txn Success", "GATEWAYNAME": "PPBLC",
        "BANKTXNID": "1234567890", "BANKNAME": "Paytm Payments Bank", "LINKDESCRIPTION": f"Invoice {inv['number']}",
        "PAYMENTEMAILID": "", "PAYMENTMOBILENUMBER": "", "PAYMENTFORM": "", "CUSTID": "",
        "MERC_UNQ_REF": inv.get("link_id") or "", "LINKNOTES": f"invoice_id={inv['id']}",
    } | extra
    f["CHECKSUMHASH"] = sign_form(f, settings.paytm_key)
    return f


async def test_the_same_notification_racing_on_both_routes_is_applied_once(api, other_merchant, db, settings):
    inv = await new_invoice(api, other_merchant, db, email=None)
    f = signed_fields(settings, inv)
    results = await asyncio.gather(*(api.post(f"/webhooks/paytm/{route}", data=f)
                                     for route in ("link", "payment", "link", "payment")))
    assert all(r.status_code == 200 for r in results)
    outcomes = sorted(r.json()["result"] for r in results)
    assert outcomes == ["duplicate", "duplicate", "duplicate", "paid"], outcomes
    assert db.execute("select count(*) as n from transactions where invoice_id = %s", (inv["id"],)).fetchone()["n"] == 1
    assert db.execute("select count(*) as n from webhook_events where external_id = %s",
                      (f["TXNID"],)).fetchone()["n"] == 1


async def test_pending_payments_panel_matches_collections_and_flips_on_payment(api, other_merchant, db, settings):
    h = bearer(other_merchant["token"])
    sent = await new_invoice(api, other_merchant, db, amount=800_00)
    overdue = await new_invoice(api, other_merchant, db, due_in=0, payer="payer_b", email=None, amount=300_00)
    cancelled = await new_invoice(api, other_merchant, db, amount=999_00)
    await api.post(f"/invoices/{cancelled['id']}/cancel", headers=h)
    db.execute("update invoices set status = 'overdue', due_date = app_today() - 9 where id = %s", (overdue["id"],))

    dash = (await api.get("/dashboard/summary", headers=h)).json()["outstanding"]
    coll = (await api.get("/invoices", headers=h)).json()
    assert dash["total_paise"] == coll["outstanding_paise"] == 1100_00
    assert dash["customers"] == coll["outstanding_customers"] == 2
    rows = {r["number"]: r for r in dash["invoices"]}
    assert set(rows) == {sent["number"], overdue["number"]} and rows[overdue["number"]]["days_overdue"] == 9

    r = await api.post("/webhooks/paytm/link", data=signed_fields(settings, overdue))
    assert r.json()["result"] == "paid"
    dash = (await api.get("/dashboard/summary", headers=h)).json()["outstanding"]
    assert dash["total_paise"] == 800_00 and dash["customers"] == 1
    assert [p["number"] for p in dash["recently_paid"]] == [overdue["number"]]
    t = dash["recently_paid"][0]
    assert t["status"] == "paid" and t["paid_at"]


async def test_reminder_kind_follows_the_app_clock_and_overdue_wording_matches_the_deck(api, other_merchant, db,
                                                                                         restore_clock):
    h = bearer(other_merchant["token"])
    inv = await new_invoice(api, other_merchant, db, due_in=1, amount=8500_00)
    email_svc.SENT.clear()
    r = await api.post(f"/invoices/{inv['id']}/remind", headers=h)
    assert r.status_code == 200 and r.json()["subject"].startswith(f"Invoice {inv['number']}")
    await api.post("/admin/clock", json={"days": 10, "run_jobs": False}, headers=h)
    r = await api.post(f"/invoices/{inv['id']}/remind", headers=h)
    assert r.status_code == 200 and r.json()["subject"] == "Reminder: ₹8,500 due to Test Store"
    html = email_svc.SENT[-1]["html"]
    assert f"invoice #{inv['number']}" in html and "is still pending (9 days past due)" in html
    assert inv["link_url"] in html and "Pay ₹8,500 with Paytm" in html and "Hi Asha ji" in html
    detail = (await api.get(f"/invoices/{inv['id']}", headers=h)).json()
    assert [x["kind"] for x in detail["reminders"]] == ["invoice", "invoice", "overdue"]


async def test_due_date_rules_use_the_app_clock_not_the_wall_clock(api, other_merchant, db, restore_clock):
    h = bearer(other_merchant["token"])
    wall_today = db.execute("select (now() at time zone 'Asia/Kolkata')::date as d").fetchone()["d"]
    await api.post("/admin/clock", json={"days": 3, "run_jobs": False}, headers=h)
    body = {"payer_id": str(other_merchant["payer_a"]), "line_items": [{"name": "x", "qty": 1, "unit_price_paise": 500_00}]}
    ok = await api.post("/invoices", json=body | {"due_date": today(db).isoformat(), "email": None}, headers=h)
    assert ok.status_code == 201, ok.text
    past = await api.post("/invoices", json=body | {"due_date": wall_today.isoformat()}, headers=h)
    assert past.status_code == 422


async def test_remind_without_any_email_is_a_clear_422(api, other_merchant, db):
    inv = await new_invoice(api, other_merchant, db, payer="payer_b", email=None)
    r = await api.post(f"/invoices/{inv['id']}/remind", headers=bearer(other_merchant["token"]))
    assert r.status_code == 422 and "has no email" in r.json()["detail"]


async def test_check_payment_on_paid_and_on_untouched_invoices(api, other_merchant, db, settings):
    h = bearer(other_merchant["token"])
    inv = await new_invoice(api, other_merchant, db, email=None)
    c = (await api.post(f"/invoices/{inv['id']}/check", headers=h)).json()
    assert c["status"] == "pending" and c["invoice"]["status"] == "sent"
    await api.post("/webhooks/paytm/link", data=signed_fields(settings, inv))
    c = (await api.post(f"/invoices/{inv['id']}/check", headers=h)).json()
    assert c["status"] == "paid" and c["result"] == "already_paid"


async def test_overdue_recurring_due_paid_on_the_mock_gateway_stops_reminding(api, other_merchant, db, settings,
                                                                             restore_clock):
    """Moment 2 + 4 together: a recurring due goes overdue, is reminded once, then the customer
    pays from the reminder's link (mock gateway → signed webhook → paid)."""
    h = bearer(other_merchant["token"])
    r = await api.post(f"/payers/{other_merchant['payer_a']}/recurring", headers=h,
                       json={"cadence": "monthly", "amount_paise": 2000_00, "grace_days": 0, "email": "asha@example.com"})
    assert r.status_code == 201
    inv = db.execute("select * from invoices where payer_id = %s", (other_merchant["payer_a"],)).fetchone()
    out = (await api.post("/admin/clock", json={"days": 1}, headers=h)).json()
    assert inv["number"] in out["jobs"]["overdue"]["reminded"]
    seen = {}

    async def deliver(path, fields):  # the gateway's S2S POST, delivered to this app
        seen["fields"] = fields
        return (await api.post(path, data=fields)).status_code

    import app.routers.mock_pay as mp

    orig = mp.send_webhook
    mp.send_webhook = deliver
    try:
        p = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success"})
    finally:
        mp.send_webhook = orig
    assert p.status_code == 200 and p.json()["webhook_status"] == 200
    row = db.execute("select status, paid_at from invoices where id = %s", (inv["id"],)).fetchone()
    assert row["status"] == "paid"
    out = (await api.post("/admin/clock", json={"days": 2}, headers=h)).json()
    assert inv["number"] not in out["jobs"]["overdue"]["overdue"]
    t = db.execute("select description_by, payer_id from transactions where invoice_id = %s", (inv["id"],)).fetchone()
    assert t["payer_id"] == other_merchant["payer_a"] and t["description_by"] == "ai"
    ids = {r["kind"] for r in db.execute("select kind from payer_identifiers where payer_id = %s",
                                         (other_merchant["payer_a"],)).fetchall()}
    assert "paytm_cust_id" in ids
