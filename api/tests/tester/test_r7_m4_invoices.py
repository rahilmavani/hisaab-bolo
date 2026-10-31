"""Tester round 7, M4: demo moment 2 re-run end to end through the API, the cancelled-invoice link, and
invoices/schedules created by a confirmed Ask Hisaab action (the only M6 path in scope).

Spec: ARCHITECTURE.md moment 2 ("an email goes out with a Paytm link and QR, the payment is made, and the
invoice flips to Paid live"), Collections flow 1-5 ("An invoice is created (from the form or a confirmed
voice action)"), Decisions/Agent ("Write actions create a pending action that needs a confirm tap"),
docs/paytm/PAYTM_INTEGRATION.md (mock contract; webhook handler order; idempotency).
Writes use the throwaway `other_merchant`; the demo merchant is re-seeded after the module.
"""

from __future__ import annotations

import secrets
from datetime import date, timedelta

import pytest
from psycopg.types.json import Jsonb

from app.services import email as email_svc

from ..conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def _reseed_after_module(reseed_demo):
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def _clean_outbox():
    email_svc.SENT.clear()
    yield


@pytest.fixture
def webhook_via_app(api, monkeypatch):
    """Route the mock gateway's webhook POST into the in-process app (as Paytm would reach the API)."""
    async def send(path, fields):
        return (await api.post(path, data=fields)).status_code
    monkeypatch.setattr("app.routers.mock_pay.send_webhook", send)


def today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


async def new_invoice(api, m, db, **extra) -> dict:
    body = {"payer_id": str(m["payer_a"]), "due_date": (today(db) + timedelta(days=7)).isoformat(),
            "line_items": [{"name": "Monthly ration", "qty": 1, "unit_price_paise": 1250_00}],
            "email": "asha@example.com"} | extra
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    return r.json()


def pending_action(db, m, tool: str, args: dict) -> str:
    token = "pa_r7_" + secrets.token_urlsafe(8)
    db.execute(
        "insert into pending_actions (token, merchant_id, tool, args, summary, expires_at) "
        "values (%s, %s, %s, %s, '{}', app_now() + interval '15 minutes')",
        (token, m["merchant_id"], tool, Jsonb(args)),
    )
    return token


# ---------------------------------------------------------------------------
# Moment 2, quickly re-verified
# ---------------------------------------------------------------------------
async def test_moment2_email_with_link_and_qr_then_gateway_payment_marks_paid_once(api, other_merchant, db,
                                                                                  webhook_via_app):
    m, h = other_merchant, bearer(other_merchant["token"])
    inv = await new_invoice(api, m, db)
    assert inv["status"] == "sent" and inv["email_error"] is None and inv["sent_at"]
    [mail] = email_svc.SENT
    assert inv["link_url"] in mail["html"] and "Pay ₹1,250 with Paytm" in mail["html"]
    assert mail["intended_to"] == "asha@example.com"
    before = (await api.get("/invoices", headers=h)).json()
    assert before["outstanding_paise"] == 1250_00

    order = inv["link_url"].rsplit("/", 1)[1]
    assert order == inv["paytm_order_id"]
    r = await api.post(f"/mock-pay/{order}", json={"outcome": "success"})
    assert r.status_code == 200 and r.json()["webhook_status"] == 200, r.text
    again = await api.post(f"/mock-pay/{order}", json={"outcome": "success"})
    assert again.status_code == 409  # one payment per link

    got = (await api.get(f"/invoices/{inv['id']}", headers=h)).json()
    assert got["status"] == "paid" and got["payment"]["amount_paise"] == 1250_00
    assert got["payment"]["paytm_txn_id"] == r.json()["txn_id"]
    txns = db.execute("select * from transactions where invoice_id = %s", (inv["id"],)).fetchall()
    assert len(txns) == 1 and txns[0]["payer_id"] == m["payer_a"] and txns[0]["paytm_order_id"] == order
    after = (await api.get("/invoices", headers=h)).json()
    assert after["outstanding_paise"] == 0 and after["outstanding_customers"] == 0


async def test_the_link_of_a_cancelled_invoice_cannot_be_paid_on_the_mock_gateway(api, other_merchant, db):
    m, h = other_merchant, bearer(other_merchant["token"])
    inv = await new_invoice(api, m, db)
    assert (await api.post(f"/invoices/{inv['id']}/cancel", headers=h)).status_code == 200
    order = inv["paytm_order_id"]
    shown = (await api.get(f"/mock-pay/{order}")).json()
    assert shown["invoice_status"] == "cancelled"
    r = await api.post(f"/mock-pay/{order}", json={"outcome": "success"})
    assert r.status_code == 409 and "cancelled" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Invoices and schedules created by a confirmed Ask Hisaab action
# ---------------------------------------------------------------------------
async def test_a_confirmed_create_invoice_action_builds_link_qr_and_email(api, other_merchant, db):
    m = other_merchant
    due = (today(db) + timedelta(days=5)).isoformat()
    token = pending_action(db, m, "create_invoice", {
        "payer_id": str(m["payer_a"]), "amount_paise": 500_00, "due_date": due,
        "line_items": [{"name": "Diwali hamper", "qty": 1, "unit_price_paise": 500_00}], "email": "asha@example.com"})
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(m["token"]))
    assert r.status_code == 200, r.text
    out = r.json()
    inv = out["invoice"]
    assert out["message"] == f"Invoice {inv['number']} created and emailed"
    assert inv["status"] == "sent" and inv["link_url"] and inv["qr_image_url"] and inv["amount_paise"] == 500_00
    assert [e["intended_to"] for e in email_svc.SENT] == ["asha@example.com"]
    again = await api.post(f"/actions/{token}/confirm", headers=bearer(m["token"]))
    assert again.status_code == 409
    assert db.execute("select count(*) as n from invoices where payer_id = %s", (m["payer_a"],)).fetchone()["n"] == 1


async def test_a_confirmed_set_recurring_action_bills_and_emails_the_first_due(api, other_merchant, db, restore_clock):
    m = other_merchant
    token = pending_action(db, m, "set_recurring", {
        "payer_id": str(m["payer_a"]), "cadence": "weekly", "amount_paise": 300_00, "email": "asha@example.com"})
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(m["token"]))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["message"] == "Marked recurring"
    [due] = out["dues_created"]
    assert due["emailed"] is True and due["due_date"] == today(db).isoformat()
    assert [e["intended_to"] for e in email_svc.SENT] == ["asha@example.com"]


# ---------------------------------------------------------------------------
# Dropped webhook + the 5-minute check, after the demo clock is reset
# ---------------------------------------------------------------------------
async def test_the_five_minute_check_finds_a_dropped_payment_on_an_invoice_made_before_a_clock_reset(
        api, other_merchant, db, settings, restore_clock):
    """mock-pay page: "The merchant wasn't notified (webhook dropped on purpose). Their “Check payment”
    button or the 5-minute check will pick it up." Risks: "cron polling covers it". The merchant jumps the
    demo clock +7 days, creates an invoice, then presses Reset; the customer pays and the webhook is lost.
    poll_open_invoices only looks at invoices created more than 2 minutes before app_now(), and this one's
    created_at is 7 days ahead of the reset clock, so the cron check skips it."""
    m, h = other_merchant, bearer(other_merchant["token"])
    assert (await api.post("/admin/clock", json={"days": 7, "run_jobs": False}, headers=h)).status_code == 200
    inv = await new_invoice(api, m, db)
    assert (await api.post("/admin/clock", json={"reset": True, "run_jobs": False}, headers=h)).status_code == 200
    r = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})
    assert r.status_code == 200 and r.json()["webhook_sent"] is False

    tick = await api.post("/jobs/tick", headers={"X-Cron-Secret": settings.cron_secret})
    assert tick.status_code == 200
    status = db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()["status"]
    assert status == "paid", (
        f"{inv['number']} was paid on the gateway (webhook dropped) but the 5-minute check skipped it: "
        f"paid_by_status_poll={tick.json()['paid_by_status_poll']}, invoice still {status!r}")
