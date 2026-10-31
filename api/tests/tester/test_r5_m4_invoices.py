"""Tester round 5, M4: invoice → paid under failures, bulk webhooks, identity on payment, link-only
invoices, and the demo clock moving back over an overdue invoice.

Spec: ARCHITECTURE.md moment 2 ("an email goes out with a Paytm link and QR, the payment is made, and
the invoice flips to Paid live"), Collections flow (steps 1-5; "Overdue (due + grace) triggers a
reminder email with the same link"), Decisions/Identity + Reconciliation, Data model (app_clock:
"every job and query uses now() from here"), docs/paytm/PAYTM_INTEGRATION.md (handler order,
idempotency on TXNID, exact ORDERID match). UI copy that states the intended behaviour:
new-invoice-dialog.tsx ("Send to (email)" placeholder "Leave empty to just get a link").

Writes use the throwaway `other_merchant`; clock jumps run the jobs for every merchant, so the demo
merchant is re-seeded after the module.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta

import pytest

from app.money import IST
from app.services import email as email_svc
from app.services.email import EmailError
from app.services.paytm import PaytmError
from app.services.paytm.checksum import sign_form

from ..conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def _reseed_after_module(reseed_demo):
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def _clean_outbox():
    """Invoice numbers restart at INV-0001 for every throwaway merchant: start each test with an empty outbox."""
    email_svc.SENT.clear()
    yield


def today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


def invoice_rows(db, merchant_id) -> list[dict]:
    return db.execute(
        "select number, status, sent_at, link_url from invoices where merchant_id = %s order by created_at",
        (merchant_id,),
    ).fetchall()


async def new_invoice(api, m, db, *, due_in=5, payer="payer_a", email="asha@example.com", amount=1500_00, **extra):
    body = {"payer_id": str(m[payer]), "due_date": (today(db) + timedelta(days=due_in)).isoformat(),
            "line_items": [{"name": "Monthly ration", "qty": 1, "unit_price_paise": amount}], "email": email} | extra
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    return r.json()


def signed_fields(settings, inv, txn_id=None, **extra) -> dict:
    now = datetime.now(IST)
    f = {
        "ORDERID": inv["paytm_order_id"], "MID": settings.paytm_mid_active,
        "TXNID": txn_id or ("R5M4" + uuid.uuid4().hex[:24].upper()),
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
# Failures while creating an invoice (Collections flow steps 1-3)
# ---------------------------------------------------------------------------
async def test_a_failed_invoice_email_does_not_leave_a_half_created_invoice(api, other_merchant, db, monkeypatch):
    """Step 3: "Resend sends the email". If Resend fails, POST /invoices answers 502 and the dialog
    shows the error and stays open. A 502 must not leave an outstanding invoice behind, or the
    merchant's retry ("Create and email" again) bills the customer twice."""
    import app.services.invoices as inv_svc

    async def resend_down(*_a, **_k):
        raise EmailError("Resend rejected the email (503): service unavailable")

    monkeypatch.setattr(inv_svc, "send_email", resend_down)
    m = other_merchant
    body = {"payer_id": str(m["payer_a"]), "due_date": (today(db) + timedelta(days=7)).isoformat(),
            "line_items": [{"name": "Monthly ration", "qty": 1, "unit_price_paise": 1500_00}],
            "email": "asha@example.com", "send_email": True}
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    if r.status_code == 201:
        return  # created and reported as such: fine
    left = invoice_rows(db, m["merchant_id"])
    outstanding = (await api.get("/invoices?status=open", headers=bearer(m["token"]))).json()
    assert not left, (
        f"POST /invoices answered {r.status_code} ({r.json().get('detail')}) but left {left} behind "
        f"(outstanding now {outstanding['outstanding_paise']} paise); retrying from the dialog creates a second invoice"
    )


async def test_a_paytm_failure_does_not_leave_an_unreachable_draft(api, other_merchant, db, monkeypatch):
    """Step 2: Paytm creates the link and QR. If that fails, POST /invoices answers 502. The invoice
    row it already inserted stays as a link-less 'draft' that Collections never lists, cannot be
    cancelled from the invoice page, and still consumed an invoice number."""
    from app.services.paytm.mock import MockPaytm

    async def paytm_down(self, req):
        raise PaytmError("503", "Service unavailable")

    monkeypatch.setattr(MockPaytm, "create_link", paytm_down)
    m = other_merchant
    body = {"payer_id": str(m["payer_a"]), "due_date": (today(db) + timedelta(days=7)).isoformat(),
            "line_items": [{"name": "Monthly ration", "qty": 1, "unit_price_paise": 1500_00}], "email": None,
            "send_email": False}
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code == 502, r.text
    left = invoice_rows(db, m["merchant_id"])
    assert not left, f"a 502 from Paytm left {left} behind (a draft with no payment link)"


# ---------------------------------------------------------------------------
# Link-only invoices (the dialog's email box cleared)
# ---------------------------------------------------------------------------
async def test_a_link_only_invoice_is_not_auto_emailed_to_the_email_on_file(api, other_merchant, db, restore_clock):
    """new-invoice-dialog.tsx: clearing "Send to (email)" ("Leave empty to just get a link") posts
    email=null, send_email=false and the button reads "Create invoice", not "Create and email". The
    merchant chose not to email this customer, so the overdue job must not email them either."""
    m = other_merchant
    inv = await new_invoice(api, m, db, due_in=0, email=None, send_email=False)
    assert not [x for x in email_svc.SENT if inv["number"] in x["html"]], "a link-only invoice was emailed on create"
    out = await jump(api, m["token"], days=2)
    reminded = out["jobs"]["overdue"]["reminded"]
    mails = [x["intended_to"] for x in email_svc.SENT if x["subject"].startswith("Reminder:") and inv["number"] in x["html"]]
    assert inv["number"] not in reminded and not mails, (
        f"{inv['number']} was created without an email (link only) but the overdue job emailed a reminder to {mails} "
        f"(invoices.email_to was filled from the payer's email on file: {inv['email_to']!r})"
    )


# ---------------------------------------------------------------------------
# Demo clock moving back over an overdue invoice
# ---------------------------------------------------------------------------
async def test_resetting_the_clock_does_not_leave_a_not_yet_due_invoice_overdue(api, other_merchant, db, restore_clock):
    """app_clock: "every job and query uses now() from here". After a +3 day jump turns an invoice
    due tomorrow overdue, Reset puts the clock back before its due date. The invoice must not still
    be 'overdue' with a negative day count (the status label then renders "Overdue · -1 days" on the
    dashboard's Pending payments, Collections and the invoice page)."""
    m = other_merchant
    inv = await new_invoice(api, m, db, due_in=1)
    out = await jump(api, m["token"], days=3)
    assert inv["number"] in out["jobs"]["overdue"]["overdue"]
    await jump(api, m["token"], reset=True)
    got = (await api.get(f"/invoices/{inv['id']}", headers=bearer(m["token"]))).json()
    dash = (await api.get("/dashboard/summary", headers=bearer(m["token"]))).json()
    row = next(r for r in dash["outstanding"]["invoices"] if r["id"] == inv["id"])
    late = -got["due_in_days"]
    assert not (got["status"] == "overdue" and late <= got["grace_days"]), (
        f"after Reset, {inv['number']} is due in {got['due_in_days']} day(s) but still status 'overdue' "
        f"(dashboard days_overdue={row['days_overdue']}); invoiceStatusLabel renders 'Overdue · {late} days'"
    )


# ---------------------------------------------------------------------------
# Bulk + idempotency
# ---------------------------------------------------------------------------
async def test_a_burst_of_webhooks_with_duplicates_pays_each_invoice_once(api, other_merchant, db, settings):
    """Paytm "may send the same notification more than once": 8 invoices, each notified 3 times
    across both routes, all at once. Each invoice is paid once with one transaction; outstanding is 0."""
    m = other_merchant
    invs = [await new_invoice(api, m, db, amount=(100 + i) * 100, email=None, send_email=False) for i in range(8)]
    sends = []
    for inv in invs:
        f = signed_fields(settings, inv)
        sends += [api.post("/webhooks/paytm/link", data=f), api.post("/webhooks/paytm/payment", data=f),
                  api.post("/webhooks/paytm/link", data=f)]
    results = await asyncio.gather(*sends)
    assert all(r.status_code == 200 for r in results), [r.text for r in results if r.status_code != 200]
    outcomes = [r.json()["result"] for r in results]
    assert outcomes.count("paid") == 8 and outcomes.count("duplicate") == 16, outcomes
    rows = db.execute(
        "select i.number, i.status, count(t.id) as n from invoices i left join transactions t on t.invoice_id = i.id "
        "where i.merchant_id = %s group by 1, 2", (m["merchant_id"],),
    ).fetchall()
    assert all(r["status"] == "paid" and r["n"] == 1 for r in rows), rows
    lst = (await api.get("/invoices", headers=bearer(m["token"]))).json()
    assert lst["outstanding_paise"] == 0 and lst["outstanding_customers"] == 0


async def test_a_late_failure_notification_does_not_reopen_a_paid_invoice(api, other_merchant, db, settings):
    m = other_merchant
    inv = await new_invoice(api, m, db, email=None, send_email=False)
    assert (await api.post("/webhooks/paytm/link", data=signed_fields(settings, inv))).json()["result"] == "paid"
    fail = signed_fields(settings, inv, STATUS="TXN_FAILURE", RESPCODE="227", RESPMSG="Declined")
    assert (await api.post("/webhooks/paytm/payment", data=fail)).status_code == 200
    got = (await api.get(f"/invoices/{inv['id']}", headers=bearer(m["token"]))).json()
    assert got["status"] == "paid" and got["payment"] is not None


# ---------------------------------------------------------------------------
# Identity on a live payment
# ---------------------------------------------------------------------------
async def test_payment_resolves_the_payer_by_order_id_and_never_steals_identifiers(api, other_merchant, db, settings):
    """Decisions: "Live payments resolve the payer via invoice order ID"; one identifier maps to one
    payer. A payment on Asha's invoice made from a phone that belongs to 'Asha T (UPI)' (payer_b)
    lands in Asha's ledger and leaves that phone with payer_b; a differently-cased email that Asha
    already owns is not added twice; a new CUSTID is added to Asha."""
    m = other_merchant
    inv = await new_invoice(api, m, db, email=None, send_email=False)
    f = signed_fields(settings, inv, PAYMENTMOBILENUMBER="+91 90000 00001", PAYMENTEMAILID="Asha@Example.COM",
                      CUSTID="PTMR5CUST01")
    out = (await api.post("/webhooks/paytm/link", data=f)).json()
    assert out["result"] == "paid"
    t = db.execute("select payer_id from transactions where invoice_id = %s", (inv["id"],)).fetchone()
    assert t["payer_id"] == m["payer_a"]
    ids = db.execute("select payer_id, kind, value from payer_identifiers where merchant_id = %s",
                     (m["merchant_id"],)).fetchall()
    phone = [r for r in ids if r["kind"] == "phone"]
    assert [(r["payer_id"], r["value"]) for r in phone] == [(m["payer_b"], "9000000001")], phone
    emails = [r for r in ids if r["kind"] == "email"]
    assert len(emails) == 1 and emails[0]["value"] == "asha@example.com", emails
    assert any(r["kind"] == "paytm_cust_id" and r["payer_id"] == m["payer_a"] for r in ids)
