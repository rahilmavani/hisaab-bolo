"""Tester round 6, M4: a dropped webhook on an invoice that is then cancelled, a failed first email
followed by the overdue reminder, a Storage failure while creating, webhooks for cancelled invoices,
identifiers from a payment.

Spec: ARCHITECTURE.md moment 2, Collections flow (steps 1-5; "Overdue (due + grace) triggers a reminder
email with the same link"), Risks ("The Paytm webhook may never arrive: a 'Check payment' button and
cron polling cover it"), Decisions/Identity ("payer_identifiers maps many identifiers to one payer"),
docs/paytm/PAYTM_INTEGRATION.md (handler order step 5: add PAYMENTMOBILENUMBER, PAYMENTEMAILID and
CUSTID to payer_identifiers; status fallback for open invoices).
UI copy that states the intended behaviour:
  collections/[id]/page.tsx  cancel confirm "The customer can still open the link, but it won't count
                             as outstanding." / "Not emailed yet: the invoice email to {email} hasn't
                             gone out." (shown when open && email_to && !sent_at)

Writes use the throwaway `other_merchant`; ticks and clock jumps sweep every merchant, so the demo
merchant is re-seeded after the module.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import httpx
import pytest

from app.money import IST
from app.services import email as email_svc
from app.services import invoices as inv_svc
from app.services.email import EmailError
from app.services.paytm.checksum import sign_form

from ..conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def _reseed_after_module(reseed_demo):
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def _clean_outbox():
    email_svc.SENT.clear()
    yield


def today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


def cron(settings) -> dict:
    return {"X-Cron-Secret": settings.cron_secret}


async def new_invoice(api, m, db, *, due_in=5, payer="payer_a", email="asha@example.com", amount=1500_00, **extra):
    body = {"payer_id": str(m[payer]), "due_date": (today(db) + timedelta(days=due_in)).isoformat(),
            "line_items": [{"name": "Monthly ration", "qty": 1, "unit_price_paise": amount}], "email": email} | extra
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    return r.json()


def signed_fields(settings, inv, **extra) -> dict:
    now = datetime.now(IST)
    f = {
        "ORDERID": inv["paytm_order_id"], "MID": settings.paytm_mid_active,
        "TXNID": "R6M4" + uuid.uuid4().hex[:24].upper(),
        "TXNAMOUNT": f"{inv['amount_paise'] / 100:.2f}", "CURRENCY": "INR", "PAYMENTMODE": "UPI",
        "TXNDATE": now.strftime("%Y-%m-%d"), "TXNDATETIME": now.strftime("%Y-%m-%d %H:%M:%S.0"),
        "STATUS": "TXN_SUCCESS", "RESPCODE": "01", "RESPMSG": "Txn Success", "GATEWAYNAME": "PPBLC",
        "BANKTXNID": "1234567890", "BANKNAME": "Paytm Payments Bank", "LINKDESCRIPTION": f"Invoice {inv['number']}",
        "PAYMENTEMAILID": "", "PAYMENTMOBILENUMBER": "", "PAYMENTFORM": "", "CUSTID": "",
        "MERC_UNQ_REF": inv.get("link_id") or "", "LINKNOTES": f"invoice_id={inv['id']}",
    } | extra
    f["CHECKSUMHASH"] = sign_form(f, settings.paytm_key)
    return f


async def jump(api, token, **body) -> dict:
    r = await api.post("/admin/clock", json=body, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Risks: "The Paytm webhook may never arrive: a Check payment button and cron polling cover it."
# ---------------------------------------------------------------------------
async def test_a_paid_order_whose_webhook_was_dropped_is_recorded_even_after_the_invoice_is_cancelled(
        api, other_merchant, db, settings):
    """The customer pays (Paytm has the money) but the webhook never arrives, so the invoice still
    looks unpaid and the merchant cancels it. The cancel dialog itself says the link stays usable.
    Paytm holds a successful payment for the invoice's order: the cron poll must still put it in the
    ledger (the invoice page shows no Check payment button once an invoice is cancelled)."""
    m = other_merchant
    inv = await new_invoice(api, m, db)
    paid = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})
    assert paid.status_code == 200 and paid.json()["status"] == "TXN_SUCCESS"

    c = await api.post(f"/invoices/{inv['id']}/cancel", headers=bearer(m["token"]))
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    db.execute("update invoices set created_at = created_at - interval '10 minutes' where id = %s", (inv["id"],))

    t = await api.post("/jobs/tick", headers=cron(settings))
    assert t.status_code == 200
    txns = db.execute("select amount_paise, paytm_txn_id from transactions where invoice_id = %s",
                      (inv["id"],)).fetchall()
    status = db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()["status"]
    assert txns, (f"Paytm took ₹{inv['amount_paise'] // 100} for {inv['number']} (txn {paid.json()['txn_id']}), "
                  f"but the ledger never records it; invoice stays {status!r}")


async def test_a_webhook_for_a_cancelled_invoice_still_records_the_money(api, other_merchant, db, settings):
    """The cancel copy says the customer can still open the link; if they pay and the webhook arrives,
    the payment must land in the ledger and the invoice must read Paid."""
    m = other_merchant
    inv = await new_invoice(api, m, db)
    assert (await api.post(f"/invoices/{inv['id']}/cancel", headers=bearer(m["token"]))).status_code == 200
    r = await api.post("/webhooks/paytm/link", data=signed_fields(settings, inv))
    assert r.status_code == 200 and r.json()["result"] == "paid"
    row = db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()
    assert row["status"] == "paid"
    assert db.execute("select count(*) as n from transactions where invoice_id = %s", (inv["id"],)).fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# Email failures and what the invoice page then says
# ---------------------------------------------------------------------------
async def test_after_the_overdue_reminder_goes_out_the_invoice_no_longer_reads_never_emailed(
        api, other_merchant, db, monkeypatch, restore_clock):
    """The first invoice email fails (Resend down), so the invoice page says "Not emailed yet: the
    invoice email to {email} hasn't gone out." and offers "Email invoice". When the due passes, the
    overdue reminder (same link and QR) is emailed. From then on that banner is false: the page derives
    it from `open && email_to && !sent_at` (collections/[id]/page.tsx:160)."""
    m = other_merchant
    real_send = inv_svc.send_email
    calls: list[str] = []

    async def first_send_fails(to, subject, html, attachments=None):
        calls.append(subject)
        if len(calls) == 1:
            raise EmailError("Resend rejected the email (500): upstream error")
        return await real_send(to, subject, html, attachments)

    monkeypatch.setattr(inv_svc, "send_email", first_send_fails)
    inv = await new_invoice(api, m, db, due_in=0)
    assert inv["email_error"] and inv["sent_at"] is None

    await jump(api, m["token"], days=1)  # one-off invoices have 0 grace days: overdue the next day
    got = (await api.get(f"/invoices/{inv['id']}", headers=bearer(m["token"]))).json()
    assert got["status"] == "overdue"
    assert [r["kind"] for r in got["reminders"]] == ["overdue"]  # the customer did get an email with the link

    page_says_never_emailed = got["status"] in ("sent", "overdue") and bool(got["email_to"]) and not got["sent_at"]
    assert not page_says_never_emailed, (
        f"{got['number']}: an overdue reminder went to {got['reminders'][0]['to_email']}, yet the page still "
        "says 'Not emailed yet: the invoice email ... hasn't gone out.' and offers 'Email invoice'")


async def test_a_storage_failure_while_creating_leaves_no_invoice_and_no_used_number(api, other_merchant, db,
                                                                                     monkeypatch):
    """Collections flow step 3 (QR PNG to Storage) failing must not leave a half-made invoice or burn a
    number; the next attempt gets the number this one would have had."""
    m = other_merchant

    async def storage_down(path, b64):
        raise httpx.ConnectError("storage unreachable")

    monkeypatch.setattr(inv_svc, "upload_qr_png", storage_down)
    body = {"payer_id": str(m["payer_a"]), "due_date": (today(db) + timedelta(days=3)).isoformat(),
            "line_items": [{"name": "Monthly ration", "qty": 1, "unit_price_paise": 1500_00}],
            "email": "asha@example.com"}
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code >= 500
    assert db.execute("select count(*) as n from invoices where merchant_id = %s", (m["merchant_id"],)).fetchone()["n"] == 0
    assert email_svc.SENT == []
    monkeypatch.undo()
    ok = await new_invoice(api, m, db, due_in=3)
    assert ok["number"] == "INV-0001" and ok["qr_image_url"] and ok["link_url"]


# ---------------------------------------------------------------------------
# Identity from a payment (handler step 5)
# ---------------------------------------------------------------------------
async def test_payment_identifiers_are_normalised_and_attached_to_the_invoice_payer(api, other_merchant, db, settings):
    m = other_merchant
    inv = await new_invoice(api, m, db, payer="payer_b", email=None)
    f = signed_fields(settings, inv, PAYMENTMOBILENUMBER="+91 98111 22334", PAYMENTEMAILID="Bina.Pays@Example.com",
                      CUSTID="PTM778899")
    r = await api.post("/webhooks/paytm/link", data=f)
    assert r.json()["result"] == "paid"
    idents = {(x["kind"], x["value"]) for x in db.execute(
        "select kind, value from payer_identifiers where payer_id = %s", (m["payer_b"],)).fetchall()}
    assert {("phone", "9811122334"), ("email", "bina.pays@example.com"), ("paytm_cust_id", "PTM778899")} <= idents
