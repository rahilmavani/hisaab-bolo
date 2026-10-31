"""M4: invoices, PaytmClient (mock), checksum, webhooks, status fallback, reminders."""

import json
import uuid
from datetime import date, timedelta

import httpx
import pytest
from paytmchecksum import PaytmChecksum

from app.services import email as email_svc
from app.services.paytm.checksum import sign_form, signed_request, verify_form

from .conftest import bearer

KEY = "HisaabMockKey#01"


# ---------------------------------------------------------------------------
# Checksum
# ---------------------------------------------------------------------------
def test_form_checksum_roundtrip_and_tamper():
    fields = {"ORDERID": "HB_INV0001_AB12CD34", "TXNID": "T1", "TXNAMOUNT": "100.00", "STATUS": "TXN_SUCCESS"}
    fields["CHECKSUMHASH"] = sign_form(fields, KEY)
    assert verify_form(fields, KEY)
    assert not verify_form({**fields, "TXNAMOUNT": "1.00"}, KEY)
    assert not verify_form(fields, "SomeOtherKey#016")
    assert not verify_form({**fields, "CHECKSUMHASH": "not-base64!!"}, KEY)
    assert not verify_form({k: v for k, v in fields.items() if k != "CHECKSUMHASH"}, KEY)


def test_signed_request_signs_exact_body_bytes():
    body = {"mid": "MID123", "orderId": "HB_INV0001_X", "amount": "8500.00", "nested": {"a": 1}}
    request, sig = signed_request(body, {"tokenType": "AES"}, KEY)
    parsed = json.loads(request)
    body_str = request[len('{"body":'):request.index(',"head":')]
    assert json.loads(body_str) == body
    assert PaytmChecksum.verifySignature(body_str, KEY, sig)
    assert parsed["head"] == {"tokenType": "AES", "signature": sig}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def webhook_via_app(api, monkeypatch):
    """Route the mock gateway's webhook POST into the in-process app."""
    async def send(path, fields):
        r = await api.post(path, data=fields)
        return r.status_code
    monkeypatch.setattr("app.routers.mock_pay.send_webhook", send)
    return send


async def make_invoice(api, m, **overrides) -> dict:
    today = date.today()
    body = {
        "payer_id": str(m["payer_a"]),
        "due_date": (today + timedelta(days=5)).isoformat(),
        "line_items": [{"name": "Canteen staples", "qty": 2, "unit_price_paise": 1500_00}],
        "email": "asha@example.com",
    } | overrides
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
async def test_create_invoice_builds_link_qr_and_email(api, other_merchant, db):
    email_svc.SENT.clear()
    inv = await make_invoice(api, other_merchant, gst_taxable=True, payer_gstin="27AAPFS1234K1ZW")
    assert inv["number"] == "INV-0001" and inv["status"] == "sent"
    assert inv["amount_paise"] == 3000_00
    assert inv["paytm_order_id"].startswith("HB_INV0001_") and len(inv["paytm_order_id"]) <= 50
    assert inv["link_id"].startswith("MOCK_LI_")
    assert inv["link_url"].endswith(f"/mock-pay/{inv['paytm_order_id']}")
    assert inv["payer_gstin"] == "27AAPFS1234K1ZW" and inv["gst_taxable"]
    png = httpx.get(inv["qr_image_url"])
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert inv["sent_at"] and inv["reminder_count"] == 1
    sent = email_svc.SENT[-1]
    assert sent["intended_to"] == "asha@example.com"
    assert "Invoice INV-0001 from Test Store" in sent["subject"]
    assert inv["link_url"] in sent["html"] and "Pay ₹3,000 with Paytm" in sent["html"]
    assert "cid:invoice-qr" in sent["html"] and sent["attachments"][0]["content_id"] == "invoice-qr"
    mock = db.execute("select * from paytm_mock_orders where order_id = %s", (inv["paytm_order_id"],)).fetchone()
    assert mock["status"] == "PENDING" and mock["amount_paise"] == 3000_00
    second = await make_invoice(api, other_merchant)
    assert second["number"] == "INV-0002"


async def test_create_invoice_without_email_only_links(api, other_merchant):
    email_svc.SENT.clear()
    inv = await make_invoice(api, other_merchant, payer_id=str(other_merchant["payer_b"]), email=None)
    assert inv["status"] == "sent" and inv["link_url"] and inv["sent_at"] is None
    assert email_svc.SENT == []
    r = await api.post(f"/invoices/{inv['id']}/remind", headers=bearer(other_merchant["token"]))
    assert r.status_code == 422 and "no email" in r.json()["detail"]


@pytest.mark.parametrize("overrides,status", [
    ({"due_date": (date.today() - timedelta(days=1)).isoformat()}, 422),
    ({"line_items": []}, 422),
    ({"line_items": [], "amount_paise": 50}, 422),
    ({"amount_paise": 1}, 422),
    ({"line_items": [{"name": "x", "qty": 1, "unit_price_paise": 500_00}], "amount_paise": 400_00}, 422),
    ({"payer_gstin": "not-a-gstin", "gst_taxable": True}, 422),
    ({"payer_id": str(uuid.uuid4())}, 404),
])
async def test_create_invoice_validation(api, other_merchant, overrides, status):
    body = {
        "payer_id": str(other_merchant["payer_a"]),
        "due_date": (date.today() + timedelta(days=3)).isoformat(),
        "line_items": [{"name": "Item", "qty": 1, "unit_price_paise": 100_00}],
    } | overrides
    r = await api.post("/invoices", json=body, headers=bearer(other_merchant["token"]))
    assert r.status_code == status, r.text


async def test_cannot_invoice_another_merchants_payer(api, other_merchant, demo_token):
    body = {"payer_id": str(other_merchant["payer_a"]), "due_date": (date.today() + timedelta(days=3)).isoformat(),
            "amount_paise": 100_00}
    r = await api.post("/invoices", json=body, headers=bearer(demo_token))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Pay via mock gateway → signed webhook → paid
# ---------------------------------------------------------------------------
async def test_mock_payment_marks_invoice_paid(api, other_merchant, webhook_via_app, db):
    inv = await make_invoice(api, other_merchant)
    page = (await api.get(f"/mock-pay/{inv['paytm_order_id']}")).json()
    assert page["amount_paise"] == 3000_00 and page["invoice_number"] == "INV-0001"
    assert page["merchant_name"] == "Test Store" and page["status"] == "PENDING"

    r = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success"})
    assert r.status_code == 200, r.text
    assert r.json()["webhook_status"] == 200

    after = (await api.get(f"/invoices/{inv['id']}", headers=bearer(other_merchant["token"]))).json()
    assert after["status"] == "paid" and after["paid_at"]
    assert after["payment"]["amount_paise"] == 3000_00 and after["payment"]["source"] == "mock"
    txns = db.execute("select * from transactions where invoice_id = %s", (inv["id"],)).fetchall()
    assert len(txns) == 1 and txns[0]["payer_id"] == other_merchant["payer_a"]
    assert txns[0]["paytm_order_id"] == inv["paytm_order_id"]
    kinds = {r["kind"] for r in db.execute(
        "select kind from payer_identifiers where payer_id = %s", (other_merchant["payer_a"],))}
    assert "paytm_cust_id" in kinds
    ev = db.execute("select * from webhook_events where external_id = %s", (txns[0]["paytm_txn_id"],)).fetchone()
    assert ev["processed_at"] and ev["error"] is None and ev["merchant_id"] == other_merchant["merchant_id"]
    again = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success"})
    assert again.status_code == 409


async def test_duplicate_webhook_is_idempotent(api, other_merchant, db):
    from app.services.paytm.mock import simulate_payment

    inv = await make_invoice(api, other_merchant)
    fields = await simulate_payment(inv["paytm_order_id"], "success")
    first = await api.post("/webhooks/paytm/link", data=fields)
    second = await api.post("/webhooks/paytm/payment", data=fields)
    assert first.json()["result"] == "paid" and second.json()["result"] == "duplicate"
    n = db.execute("select count(*) as n from transactions where invoice_id = %s", (inv["id"],)).fetchone()["n"]
    assert n == 1


async def test_bad_checksum_rejected(api, other_merchant, db):
    from app.services.paytm.mock import simulate_payment

    inv = await make_invoice(api, other_merchant)
    fields = await simulate_payment(inv["paytm_order_id"], "success")
    tampered = {**fields, "TXNAMOUNT": "1.00"}
    r = await api.post("/webhooks/paytm/link", data=tampered)
    assert r.status_code == 400
    assert db.execute("select 1 from webhook_events where external_id = %s", (fields["TXNID"],)).fetchone() is None
    r = await api.post("/webhooks/paytm/link", data={k: v for k, v in fields.items() if k != "CHECKSUMHASH"})
    assert r.status_code == 400
    status = db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()["status"]
    assert status == "sent"


async def test_amount_mismatch_is_not_marked_paid(api, other_merchant, db):
    from app.services.paytm.mock import simulate_payment

    inv = await make_invoice(api, other_merchant)
    fields = await simulate_payment(inv["paytm_order_id"], "success")
    fields["TXNAMOUNT"] = "2999.00"
    fields["CHECKSUMHASH"] = sign_form(fields, KEY)
    r = await api.post("/webhooks/paytm/link", data=fields)
    assert r.status_code == 200 and r.json()["result"] == "amount_mismatch"
    assert db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()["status"] == "sent"
    ev = db.execute("select error from webhook_events where external_id = %s", (fields["TXNID"],)).fetchone()
    assert "TXNAMOUNT" in ev["error"]


async def test_failed_payment_keeps_invoice_open(api, other_merchant, webhook_via_app, db):
    inv = await make_invoice(api, other_merchant)
    r = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "failure"})
    assert r.json()["status"] == "TXN_FAILURE"
    row = db.execute("select status, paytm_raw from invoices where id = %s", (inv["id"],)).fetchone()
    assert row["status"] == "sent" and row["paytm_raw"]["last_failure"]["STATUS"] == "TXN_FAILURE"


async def test_lost_webhook_recovered_by_check_payment(api, other_merchant, webhook_via_app, db):
    inv = await make_invoice(api, other_merchant)
    h = bearer(other_merchant["token"])
    r = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})
    assert r.json()["webhook_sent"] is False
    assert (await api.get(f"/invoices/{inv['id']}", headers=h)).json()["status"] == "sent"
    check = (await api.post(f"/invoices/{inv['id']}/check", headers=h)).json()
    assert check["status"] == "paid" and check["invoice"]["status"] == "paid"
    assert (await api.post(f"/invoices/{inv['id']}/check", headers=h)).json()["result"] == "already_paid"
    n = db.execute("select count(*) as n from transactions where invoice_id = %s", (inv["id"],)).fetchone()["n"]
    assert n == 1


async def test_check_payment_when_unpaid(api, other_merchant):
    inv = await make_invoice(api, other_merchant)
    r = await api.post(f"/invoices/{inv['id']}/check", headers=bearer(other_merchant["token"]))
    assert r.json()["status"] == "pending" and r.json()["invoice"]["status"] == "sent"


async def test_poll_open_invoices_settles_lost_webhook(api, other_merchant, db):
    from app.db import connection
    from app.services.payments import poll_open_invoices
    from app.services.paytm.mock import simulate_payment

    inv = await make_invoice(api, other_merchant)
    await simulate_payment(inv["paytm_order_id"], "success")
    db.execute("update invoices set created_at = created_at - interval '10 minutes' where id = %s", (inv["id"],))
    async with connection() as conn:
        settled = await poll_open_invoices(conn)
    assert any(s["invoice"] == inv["number"] for s in settled)
    assert db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()["status"] == "paid"


# ---------------------------------------------------------------------------
# Reminders, listing, cancel
# ---------------------------------------------------------------------------
async def test_overdue_reminder_email(api, other_merchant, db):
    email_svc.SENT.clear()
    inv = await make_invoice(api, other_merchant)
    db.execute("update invoices set due_date = current_date - 4, status = 'overdue' where id = %s", (inv["id"],))
    r = await api.post(f"/invoices/{inv['id']}/remind", headers=bearer(other_merchant["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["reminder"]["kind"] == "overdue"
    sent = email_svc.SENT[-1]
    assert sent["subject"] == "Reminder: ₹3,000 due to Test Store"
    assert "Hi Asha ji" in sent["html"] and "is still pending" in sent["html"] and inv["link_url"] in sent["html"]
    detail = (await api.get(f"/invoices/{inv['id']}", headers=bearer(other_merchant["token"]))).json()
    assert [x["kind"] for x in detail["reminders"]] == ["invoice", "overdue"]


async def test_list_and_cancel(api, other_merchant):
    h = bearer(other_merchant["token"])
    a = await make_invoice(api, other_merchant)
    listed = (await api.get("/invoices?status=open", headers=h)).json()
    assert listed["outstanding_paise"] == 3000_00 and listed["outstanding_customers"] == 1
    r = await api.post(f"/invoices/{a['id']}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await api.post(f"/invoices/{a['id']}/remind", headers=h)).status_code == 409
    assert (await api.get("/invoices?status=open", headers=h)).json()["outstanding_paise"] == 0


async def test_mock_pay_unknown_order(api):
    assert (await api.get("/mock-pay/NOPE_123")).status_code == 404
    assert (await api.post("/mock-pay/NOPE_123", json={"outcome": "success"})).status_code == 404


async def test_invoice_routes_need_auth(api):
    assert (await api.get("/invoices")).status_code == 401
    assert (await api.post("/invoices", json={})).status_code == 401


# ---------------------------------------------------------------------------
# Real Paytm staging contract (run with `pytest -m staging` once keys exist)
# ---------------------------------------------------------------------------
@pytest.mark.staging
async def test_staging_create_link_qr_and_status(settings):
    from app.services.paytm import LinkRequest, QrRequest, RealPaytm

    client = RealPaytm("staging", settings.paytm_mid, settings.paytm_merchant_key, settings.public_api_base)
    order = f"HB_TEST_{uuid.uuid4().hex[:10].upper()}"
    link = await client.create_link(LinkRequest(order_id=order, amount_paise=100_00, invoice_number=order,
                                                 link_name="HisaabBoloTest", description="Contract test",
                                                 expiry=date.today() + timedelta(days=7), payer_name="Test"))
    assert link.link_id and link.short_url
    from datetime import datetime, time

    qr = await client.create_qr(QrRequest(order_id=order, amount_paise=100_00, display_name="Hisaab Bolo",
                                          invoice_number=order,
                                          expiry=datetime.combine(date.today() + timedelta(days=7), time(23, 59))))
    assert qr.qr_data and qr.image_b64
    st = await client.order_status(order)
    assert st.status in ("PENDING", "NO_RECORD")
