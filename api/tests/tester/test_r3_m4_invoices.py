"""Tester round 3, M4: invoices, PaytmClient (mock + real request shapes), checksum, webhooks,
idempotency, reconciliation, Status-API fallback, polling, reminders, cancel and isolation.

Spec: ARCHITECTURE.md (moment 2 "Invoice to paid", Decisions/Identity + Reconciliation + GST,
API surface, Collections flow, Risks) and docs/paytm/PAYTM_INTEGRATION.md (checksum rules,
create link / QR rules, webhook handler order, idempotency, Transaction Status fallback,
mock-mode contract).

All writes use the throwaway `other_merchant`; nothing touches the demo merchant.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from paytmchecksum import PaytmChecksum

from app.money import IST
from app.services import email as email_svc
from app.services.paytm.checksum import sign_form
from app.services.qr import qr_png_b64

from ..conftest import bearer

LINK_WEBHOOK_FIELDS = {
    "ORDERID", "MID", "TXNID", "TXNAMOUNT", "CURRENCY", "PAYMENTMODE", "TXNDATE", "TXNDATETIME", "STATUS",
    "RESPCODE", "RESPMSG", "GATEWAYNAME", "BANKTXNID", "BANKNAME", "CHECKSUMHASH", "LINKDESCRIPTION",
    "PAYMENTEMAILID", "PAYMENTMOBILENUMBER", "PAYMENTFORM", "CUSTID", "MERC_UNQ_REF", "LINKNOTES",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def code_of(api, method, path, **kw) -> int:
    """Status code; an exception escaping the app (a real server's 500) counts as 500."""
    try:
        return (await api.request(method, path, **kw)).status_code
    except Exception:  # noqa: BLE001
        return 500


def app_today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


async def make_invoice(api, m, **overrides) -> dict:
    body = {
        "payer_id": str(m["payer_a"]),
        "due_date": (date.today() + timedelta(days=5)).isoformat(),
        "line_items": [{"name": "Canteen staples", "qty": 2, "unit_price_paise": 1500_00}],
        "email": "asha@example.com",
    } | overrides
    r = await api.post("/invoices", json=body, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    return r.json()


def webhook_fields(settings, inv: dict, *, txn_id: str | None = None, amount: str | None = None,
                   status: str = "TXN_SUCCESS", **extra) -> dict:
    now = datetime.now(IST)
    f = {
        "ORDERID": inv["paytm_order_id"],
        "MID": settings.paytm_mid_active,
        "TXNID": txn_id or ("20261031" + uuid.uuid4().hex[:22].upper()),
        "TXNAMOUNT": amount if amount is not None else f"{inv['amount_paise'] / 100:.2f}",
        "CURRENCY": "INR",
        "PAYMENTMODE": "UPI",
        "TXNDATE": now.strftime("%Y-%m-%d"),
        "TXNDATETIME": now.strftime("%Y-%m-%d %H:%M:%S.0"),
        "STATUS": status,
        "RESPCODE": "01" if status == "TXN_SUCCESS" else "227",
        "RESPMSG": "Txn Success" if status == "TXN_SUCCESS" else "Declined",
        "GATEWAYNAME": "PPBLC",
        "BANKTXNID": "777000111222",
        "BANKNAME": "Paytm Payments Bank",
        "LINKDESCRIPTION": f"Invoice {inv['number']}",
        "PAYMENTEMAILID": "",
        "PAYMENTMOBILENUMBER": "",
        "PAYMENTFORM": "",
        "CUSTID": "",
        "MERC_UNQ_REF": inv.get("link_id") or "",
        "LINKNOTES": f"invoice_id={inv['id']}",
    }
    f.update(extra)
    return f


def signed(fields: dict, key: str) -> dict:
    return {**fields, "CHECKSUMHASH": sign_form(fields, key)}


async def post_webhook(api, fields: dict, route: str = "link") -> httpx.Response:
    return await api.post(f"/webhooks/paytm/{route}", data=fields)


def inv_row(db, inv_id) -> dict:
    return db.execute("select * from invoices where id = %s", (inv_id,)).fetchone()


def txns_for(db, inv_id) -> list[dict]:
    return db.execute("select * from transactions where invoice_id = %s", (inv_id,)).fetchall()


# ---------------------------------------------------------------------------
# Numbering, order ID, link, QR, storage
# ---------------------------------------------------------------------------
async def test_numbering_order_id_link_and_mock_contract(api, other_merchant, db, settings):
    a = await make_invoice(api, other_merchant, email=None)
    b = await make_invoice(api, other_merchant, email=None)
    assert (a["number"], b["number"]) == ("INV-0001", "INV-0002")
    for inv in (a, b):
        oid = inv["paytm_order_id"]
        assert re.fullmatch(r"HB_INV\d{4}_[0-9A-F]{8}", oid), oid
        assert len(oid) <= 50
        assert inv["qr_order_id"] is None  # one order ID per invoice (mock accepts the shared ID)
        assert re.fullmatch(r"MOCK_LI_\d+", inv["link_id"])
        assert inv["link_url"] == f"{settings.public_web_base}/mock-pay/{oid}"
        assert inv["status"] == "sent"
    assert a["paytm_order_id"] != b["paytm_order_id"] and a["link_id"] != b["link_id"]
    seq = db.execute("select invoice_seq from merchants where id = %s", (other_merchant["merchant_id"],)).fetchone()
    assert seq["invoice_seq"] == 2


async def test_qr_png_is_public_and_encodes_the_upi_order(api, other_merchant, settings):
    inv = await make_invoice(api, other_merchant, email=None)
    url = inv["qr_image_url"]
    assert f"/storage/v1/object/public/qr/{other_merchant['merchant_id']}/" in url
    png = httpx.get(url, timeout=10)  # no apikey / auth header: must be public
    assert png.status_code == 200 and png.headers["content-type"].startswith("image/png")
    qr_data = inv["paytm_raw"]["qr_data"]
    q = parse_qs(urlparse(qr_data).query)
    assert qr_data.startswith("upi://pay?")
    assert q["pa"] == ["mock@paytm"] and q["am"] == ["3000.00"] and q["tr"] == [inv["paytm_order_id"]]
    # The stored PNG is exactly the QR for that UPI string.
    assert png.content == base64.b64decode(qr_png_b64(qr_data))


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
async def test_b2b_invoice_email_has_pay_link_qr_gstin_and_intended_recipient(api, other_merchant, settings):
    email_svc.SENT.clear()
    inv = await make_invoice(api, other_merchant, gst_taxable=True, payer_gstin="27AAPFS1234K1ZW",
                             email="Accounts@Asha.example")
    assert len(email_svc.SENT) == 1
    msg = email_svc.SENT[-1]
    assert msg["intended_to"] == "accounts@asha.example"
    assert msg["to"] == (settings.demo_customer_email or "accounts@asha.example")
    html = msg["html"]
    assert "To: accounts@asha.example" in html
    assert f'href="{inv["link_url"]}"' in html and "Pay ₹3,000 with Paytm" in html
    assert "or scan with any UPI app" in html
    assert "27AAPFS1234K1ZW" in html and "GST invoice" in html
    assert "INV-0001" in msg["subject"] and "Test Store" in msg["subject"]
    # QR: either the public URL or an inline CID attachment carrying the same PNG.
    if "cid:invoice-qr" in html:
        att = msg["attachments"][0]
        assert base64.b64decode(att["content"]) == httpx.get(inv["qr_image_url"]).content
    else:
        assert inv["qr_image_url"] in html


async def test_non_gst_invoice_email_has_no_gstin(api, other_merchant):
    email_svc.SENT.clear()
    await make_invoice(api, other_merchant, gst_taxable=False, payer_gstin="27AAPFS1234K1ZW")
    html = email_svc.SENT[-1]["html"]
    assert "GSTIN" not in html and "GST invoice" not in html


async def test_line_item_names_are_html_escaped_in_email(api, other_merchant):
    email_svc.SENT.clear()
    await make_invoice(api, other_merchant, line_items=[{"name": "<b>Atta</b> & dal", "qty": 1, "unit_price_paise": 500_00}])
    html = email_svc.SENT[-1]["html"]
    assert "<b>Atta</b>" not in html and "&lt;b&gt;Atta&lt;/b&gt; &amp; dal" in html


# ---------------------------------------------------------------------------
# Checksum on both webhook routes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("route", ["link", "payment"])
@pytest.mark.parametrize("mutate", ["tampered", "missing", "wrong_key", "garbage", "json_body"])
async def test_webhook_rejects_bad_checksum(api, other_merchant, db, settings, route, mutate):
    inv = await make_invoice(api, other_merchant, email=None)
    f = webhook_fields(settings, inv)
    good = signed(f, settings.paytm_key)
    if mutate == "tampered":
        r = await post_webhook(api, {**good, "TXNAMOUNT": "1.00"}, route)
    elif mutate == "missing":
        r = await post_webhook(api, f, route)
    elif mutate == "wrong_key":
        r = await post_webhook(api, signed(f, "WrongMockKey#016"), route)
    elif mutate == "garbage":
        r = await post_webhook(api, {**f, "CHECKSUMHASH": "%%%not-base64%%%"}, route)
    else:
        r = await api.post(f"/webhooks/paytm/{route}", json=good)
    assert r.status_code == 400, r.text
    assert inv_row(db, inv["id"])["status"] == "sent"
    assert db.execute("select count(*) as n from webhook_events where external_id = %s", (f["TXNID"],)).fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Idempotency, matching, amount, failures
# ---------------------------------------------------------------------------
async def test_same_txnid_is_processed_once_across_both_routes(api, other_merchant, db, settings):
    inv = await make_invoice(api, other_merchant, email=None)
    f = signed(webhook_fields(settings, inv), settings.paytm_key)
    r1 = await post_webhook(api, f, "link")
    r2 = await post_webhook(api, f, "payment")
    r3 = await post_webhook(api, f, "link")
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert r1.json()["result"] == "paid"
    assert r2.json()["result"] == "duplicate" and r3.json()["result"] == "duplicate"
    assert len(txns_for(db, inv["id"])) == 1
    n = db.execute("select count(*) as n from webhook_events where external_id = %s", (f["TXNID"],)).fetchone()["n"]
    assert n == 1


@pytest.mark.parametrize("variant", ["lower", "suffix", "prefix", "spaces"])
async def test_orderid_must_match_exactly(api, other_merchant, db, settings, variant):
    inv = await make_invoice(api, other_merchant, email=None)
    oid = inv["paytm_order_id"]
    bad = {"lower": oid.lower(), "suffix": oid + "X", "prefix": oid[:-1], "spaces": f" {oid} "}[variant]
    f = signed(webhook_fields(settings, inv, ORDERID=bad, MERC_UNQ_REF=""), settings.paytm_key)
    r = await post_webhook(api, f)
    assert r.status_code == 200 and r.json()["result"] == "unmatched"
    assert inv_row(db, inv["id"])["status"] == "sent" and txns_for(db, inv["id"]) == []


async def test_orderid_wins_over_another_invoices_link_id(api, other_merchant, db, settings):
    a = await make_invoice(api, other_merchant, email=None)
    b = await make_invoice(api, other_merchant, email=None, line_items=[], amount_paise=3000_00)
    f = signed(webhook_fields(settings, a, MERC_UNQ_REF=b["link_id"]), settings.paytm_key)
    assert (await post_webhook(api, f)).json()["result"] == "paid"
    assert inv_row(db, a["id"])["status"] == "paid" and inv_row(db, b["id"])["status"] == "sent"


@pytest.mark.parametrize("amount,ok", [
    ("3000", True), ("3000.00", True), ("3000.0", True),
    ("2999.99", False), ("3000.01", False), ("300.00", False), ("3,000.00", False), ("", False),
    ("Infinity", False), ("NaN", False), ("-3000.00", False),
])
async def test_amount_must_equal_invoice_amount(api, other_merchant, db, settings, amount, ok):
    inv = await make_invoice(api, other_merchant, email=None)
    f = signed(webhook_fields(settings, inv, amount=amount), settings.paytm_key)
    code = await code_of(api, "POST", "/webhooks/paytm/link", data=f)
    assert code == 200, f"TXNAMOUNT={amount!r} -> {code}"
    assert (inv_row(db, inv["id"])["status"] == "paid") is ok


async def test_failure_webhook_keeps_invoice_open_then_success_pays(api, other_merchant, db, settings):
    inv = await make_invoice(api, other_merchant, email=None)
    fail = signed(webhook_fields(settings, inv, status="TXN_FAILURE"), settings.paytm_key)
    r = await post_webhook(api, fail)
    assert r.status_code == 200 and r.json()["result"] == "failure_recorded"
    row = inv_row(db, inv["id"])
    assert row["status"] == "sent" and row["paid_at"] is None and txns_for(db, inv["id"]) == []
    assert row["paytm_raw"]["last_failure"]["TXNID"] == fail["TXNID"]
    ok = signed(webhook_fields(settings, inv), settings.paytm_key)
    assert (await post_webhook(api, ok)).json()["result"] == "paid"
    assert inv_row(db, inv["id"])["status"] == "paid"


async def test_paid_webhook_writes_invoice_transaction_and_identifiers(api, other_merchant, db, settings):
    inv = await make_invoice(api, other_merchant, payer_id=str(other_merchant["payer_b"]), email=None)
    f = signed(webhook_fields(settings, inv, PAYMENTMOBILENUMBER="+91 98111 22333",
                              PAYMENTEMAILID="asha.t@example.com", CUSTID="PTM1234567890",
                              TXNDATETIME="2026-10-31 11:05:09.0"), settings.paytm_key)
    r = await post_webhook(api, f)
    assert r.json()["result"] == "paid"
    row = inv_row(db, inv["id"])
    assert row["status"] == "paid"
    assert row["paid_at"] == datetime(2026, 10, 31, 11, 5, 9, tzinfo=IST)
    [t] = txns_for(db, inv["id"])
    assert t["payer_id"] == other_merchant["payer_b"] and t["merchant_id"] == other_merchant["merchant_id"]
    assert t["amount_paise"] == inv["amount_paise"] and t["paytm_txn_id"] == f["TXNID"]
    assert t["paytm_order_id"] == inv["paytm_order_id"] and t["payment_mode"] == "UPI"
    assert t["source"] == "mock" and t["paid_at"] == row["paid_at"]
    assert "CHECKSUMHASH" not in t["raw"] and t["raw"]["TXNID"] == f["TXNID"]
    ids = {(i["kind"], i["value"]) for i in db.execute(
        "select kind, value from payer_identifiers where payer_id = %s", (other_merchant["payer_b"],)).fetchall()}
    assert {("phone", "9811122333"), ("email", "asha.t@example.com"), ("paytm_cust_id", "PTM1234567890")} <= ids
    ev = db.execute("select * from webhook_events where external_id = %s", (f["TXNID"],)).fetchone()
    assert ev["processed_at"] and ev["error"] is None and ev["merchant_id"] == other_merchant["merchant_id"]


async def test_two_different_payments_race_for_one_invoice(api, other_merchant, db, settings):
    inv = await make_invoice(api, other_merchant, email=None)
    f1 = signed(webhook_fields(settings, inv), settings.paytm_key)
    f2 = signed(webhook_fields(settings, inv), settings.paytm_key)
    r1, r2 = await asyncio.gather(post_webhook(api, f1, "link"), post_webhook(api, f2, "payment"))
    assert sorted([r1.json()["result"], r2.json()["result"]]) == ["already_paid", "paid"]
    assert len(txns_for(db, inv["id"])) == 1


# ---------------------------------------------------------------------------
# Status-API fallback ("Check payment") and the cron-style poll
# ---------------------------------------------------------------------------
async def test_check_payment_settles_a_dropped_webhook_via_status_api(api, other_merchant, db):
    inv = await make_invoice(api, other_merchant, email=None)
    r = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})
    assert r.status_code == 200 and r.json()["webhook_sent"] is False
    assert inv_row(db, inv["id"])["status"] == "sent"
    c = await api.post(f"/invoices/{inv['id']}/check", headers=bearer(other_merchant["token"]))
    assert c.status_code == 200 and c.json()["status"] == "paid" and c.json()["invoice"]["status"] == "paid"
    ev = db.execute("select route from webhook_events where external_id = %s", (r.json()["txn_id"],)).fetchone()
    assert ev["route"] == "status_api"
    [t] = txns_for(db, inv["id"])
    assert t["paytm_txn_id"] == r.json()["txn_id"]


async def test_check_payment_after_failed_attempt_reports_not_paid(api, other_merchant, db):
    inv = await make_invoice(api, other_merchant, email=None)
    await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "failure"})
    c = (await api.post(f"/invoices/{inv['id']}/check", headers=bearer(other_merchant["token"]))).json()
    assert c["status"] != "paid" and c["invoice"]["status"] == "sent"


async def test_check_payment_never_says_paid_for_an_unpaid_invoice(api, other_merchant, db, settings):
    """A webhook for the same TXNID that was recorded but did not pay the invoice (here: an amount
    mismatch) must not make "Check payment" report "paid" while the invoice stays unpaid."""
    inv = await make_invoice(api, other_merchant, email=None)
    paid = (await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})).json()
    bad = signed(webhook_fields(settings, inv, txn_id=paid["txn_id"], amount="1.00"), settings.paytm_key)
    assert (await post_webhook(api, bad)).json()["result"] == "amount_mismatch"
    c = (await api.post(f"/invoices/{inv['id']}/check", headers=bearer(other_merchant["token"]))).json()
    assert not (c["status"] == "paid" and c["invoice"]["status"] != "paid"), (
        f"check said {c['status']!r} ({c.get('result')}) but the invoice is {c['invoice']['status']!r}")


async def test_payment_survives_a_crash_during_the_paid_write(api, other_merchant, db, settings, monkeypatch):
    """Handler step 5 fails once (e.g. a dropped DB connection). The one-transaction write must roll
    back, and Paytm's retry of the same notification or "Check payment" must still settle it."""
    import app.services.payments as payments

    inv = await make_invoice(api, other_merchant, email=None)
    paid = (await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})).json()
    f = signed(webhook_fields(settings, inv, txn_id=paid["txn_id"], PAYMENTMOBILENUMBER="9811100000"),
               settings.paytm_key)
    real_add = payments.add_identifier
    calls = {"n": 0}

    async def flaky_add(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated connection drop")
        return await real_add(*a, **kw)

    monkeypatch.setattr(payments, "add_identifier", flaky_add)
    first = await code_of(api, "POST", "/webhooks/paytm/link", data=f)
    assert first >= 500
    row = inv_row(db, inv["id"])
    assert row["status"] == "sent" and txns_for(db, inv["id"]) == []  # atomic: nothing half-written

    retry = await post_webhook(api, f)  # Paytm re-sends the same notification
    check = (await api.post(f"/invoices/{inv['id']}/check", headers=bearer(other_merchant["token"]))).json()
    final = inv_row(db, inv["id"])["status"]
    assert final == "paid", (
        f"payment lost: retry -> {retry.json().get('result')!r}, check -> {check['status']!r}/{check.get('result')!r}, "
        f"invoice still {final!r}")


async def test_poll_open_invoices_only_picks_open_invoices_older_than_two_minutes(api, other_merchant, db):
    from app.db import connection
    from app.services.payments import poll_open_invoices

    old = await make_invoice(api, other_merchant, email=None)
    fresh = await make_invoice(api, other_merchant, email=None)
    cancelled = await make_invoice(api, other_merchant, email=None)
    await api.post(f"/invoices/{cancelled['id']}/cancel", headers=bearer(other_merchant["token"]))
    for inv in (old, fresh):
        r = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})
        assert r.status_code == 200
    # The payment page refuses cancelled invoices, so settle that order at the gateway directly.
    from app.services.paytm.mock import simulate_payment

    await simulate_payment(cancelled["paytm_order_id"], "success")
    db.execute("update invoices set created_at = app_now() - interval '3 minutes' where id = any(%s)",
               ([old["id"], cancelled["id"]],))
    async with connection() as conn:
        out = await poll_open_invoices(conn)
    settled = {r["invoice"] for r in out}
    assert old["number"] in settled
    assert fresh["number"] not in settled
    assert inv_row(db, old["id"])["status"] == "paid"
    assert inv_row(db, fresh["id"])["status"] == "sent"
    # Superseded by tester round 6: cancelling doesn't deactivate the Paytm link, so money paid on a
    # cancelled invoice's link must still reach the ledger through the poll (Risks: "cron polling covers it").
    assert cancelled["number"] in settled and inv_row(db, cancelled["id"])["status"] == "paid"


# ---------------------------------------------------------------------------
# Mock gateway contract
# ---------------------------------------------------------------------------
async def test_mock_gateway_sends_the_exact_signed_link_field_set(api, other_merchant, db, settings, monkeypatch):
    inv = await make_invoice(api, other_merchant, payer_id=str(other_merchant["payer_b"]), email=None)
    captured = {}

    async def capture(path, fields):
        captured["path"], captured["fields"] = path, dict(fields)
        return (await api.post(path, data=fields)).status_code

    monkeypatch.setattr("app.routers.mock_pay.send_webhook", capture)
    r = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success"})
    assert r.status_code == 200 and r.json()["webhook_status"] == 200
    f = captured["fields"]
    assert captured["path"] == "/webhooks/paytm/link"
    assert set(f) == LINK_WEBHOOK_FIELDS
    assert f["ORDERID"] == inv["paytm_order_id"] and f["MERC_UNQ_REF"] == inv["link_id"]
    assert f["TXNAMOUNT"] == "3000.00" and f["STATUS"] == "TXN_SUCCESS" and f["MID"] == settings.paytm_mock_mid
    body = {k: v for k, v in f.items() if k != "CHECKSUMHASH"}
    assert PaytmChecksum.verifySignature(body, settings.paytm_mock_key, f["CHECKSUMHASH"])
    assert inv_row(db, inv["id"])["status"] == "paid"
    again = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success"})
    assert again.status_code == 409
    page = (await api.get(f"/mock-pay/{inv['paytm_order_id']}")).json()
    assert page["status"] == "TXN_SUCCESS" and page["txn_id"] == f["TXNID"]


async def test_mock_gateway_is_off_outside_mock_mode(api, other_merchant, settings, monkeypatch):
    inv = await make_invoice(api, other_merchant, email=None)
    monkeypatch.setattr(settings, "paytm_mode", "staging")
    assert (await api.get(f"/mock-pay/{inv['paytm_order_id']}")).status_code == 404
    assert (await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success"})).status_code == 404


async def test_mock_gateway_rejects_bad_outcome_and_unknown_order(api, other_merchant):
    inv = await make_invoice(api, other_merchant, email=None)
    assert (await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "maybe"})).status_code == 422
    assert (await api.get("/mock-pay/HB_INV9999_DEADBEEF")).status_code == 404
    assert (await api.post("/mock-pay/HB_INV9999_DEADBEEF", json={"outcome": "success"})).status_code == 404


# ---------------------------------------------------------------------------
# Reminders and cancel
# ---------------------------------------------------------------------------
async def test_overdue_reminder_uses_same_link_and_records_history(api, other_merchant, db):
    inv = await make_invoice(api, other_merchant)
    db.execute("update invoices set due_date = app_today() - 4, status = 'overdue' where id = %s", (inv["id"],))
    email_svc.SENT.clear()
    r = await api.post(f"/invoices/{inv['id']}/remind", headers=bearer(other_merchant["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intended_to"] == "asha@example.com" and body["reminder"]["kind"] == "overdue"
    msg = email_svc.SENT[-1]
    assert msg["subject"] == "Reminder: ₹3,000 due to Test Store"
    assert "Hi Asha ji, invoice #INV-0001" in msg["html"] and "4 days past due" in msg["html"]
    assert inv["link_url"] in msg["html"] and "Pay ₹3,000 with Paytm" in msg["html"]
    detail = (await api.get(f"/invoices/{inv['id']}", headers=bearer(other_merchant["token"]))).json()
    assert [x["kind"] for x in detail["reminders"]] == ["invoice", "overdue"]
    assert detail["reminder_count"] == 2


async def test_remind_refuses_paid_and_cancelled(api, other_merchant, settings):
    paid = await make_invoice(api, other_merchant)
    await post_webhook(api, signed(webhook_fields(settings, paid), settings.paytm_key))
    gone = await make_invoice(api, other_merchant)
    await api.post(f"/invoices/{gone['id']}/cancel", headers=bearer(other_merchant["token"]))
    for inv in (paid, gone):
        r = await api.post(f"/invoices/{inv['id']}/remind", headers=bearer(other_merchant["token"]))
        assert r.status_code == 409, r.text


async def test_cancel_open_but_not_paid(api, other_merchant, settings, db):
    inv = await make_invoice(api, other_merchant, email=None)
    r = await api.post(f"/invoices/{inv['id']}/cancel", headers=bearer(other_merchant["token"]))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    lst = (await api.get("/invoices", headers=bearer(other_merchant["token"]))).json()
    assert lst["outstanding_paise"] == 0
    paid = await make_invoice(api, other_merchant, email=None)
    await post_webhook(api, signed(webhook_fields(settings, paid), settings.paytm_key))
    r = await api.post(f"/invoices/{paid['id']}/cancel", headers=bearer(other_merchant["token"]))
    assert r.status_code == 409


async def test_cancel_unknown_invoice_is_404(api, other_merchant):
    r = await api.post(f"/invoices/{uuid.uuid4()}/cancel", headers=bearer(other_merchant["token"]))
    assert r.status_code == 404, f"{r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# Auth and isolation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,suffix", [
    ("GET", ""), ("POST", "/remind"), ("POST", "/check"), ("POST", "/cancel"),
])
async def test_another_merchants_invoice_is_404(api, other_merchant, demo_token, db, method, suffix):
    inv = await make_invoice(api, other_merchant, email=None)
    r = await api.request(method, f"/invoices/{inv['id']}{suffix}", headers=bearer(demo_token))
    assert r.status_code == 404, f"{method} {suffix} -> {r.status_code} {r.text}"
    assert inv_row(db, inv["id"])["status"] == "sent"


async def test_invoice_list_is_scoped_to_the_merchant(api, other_merchant, demo_token):
    inv = await make_invoice(api, other_merchant, email=None)
    demo = (await api.get("/invoices", headers=bearer(demo_token))).json()
    assert inv["id"] not in {i["id"] for i in demo["invoices"]}
    mine = (await api.get("/invoices", headers=bearer(other_merchant["token"]))).json()
    assert [i["id"] for i in mine["invoices"]] == [inv["id"]]
    assert mine["outstanding_paise"] == 3000_00 and mine["outstanding_customers"] == 1


@pytest.mark.parametrize("method,path", [
    ("GET", "/invoices"), ("POST", "/invoices"), ("GET", "/invoices/{id}"),
    ("POST", "/invoices/{id}/remind"), ("POST", "/invoices/{id}/check"), ("POST", "/invoices/{id}/cancel"),
])
async def test_invoice_routes_reject_missing_and_forged_tokens(api, other_merchant, method, path):
    inv = await make_invoice(api, other_merchant, email=None)
    p = path.format(id=inv["id"])
    assert (await api.request(method, p)).status_code == 401
    forged = other_merchant["token"][:-4] + ("AAAA" if not other_merchant["token"].endswith("AAAA") else "BBBB")
    assert (await api.request(method, p, headers=bearer(forged))).status_code == 401


# ---------------------------------------------------------------------------
# Identity (Decisions: one ledger per person; one identifier belongs to one payer)
# ---------------------------------------------------------------------------
async def test_invoice_email_cannot_copy_another_customers_email_onto_a_payer(api, other_merchant, db):
    m = other_merchant
    db.execute("insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s, %s, 'email', %s)",
               (m["payer_a"], m["merchant_id"], "asha@example.com"))
    # M2's rule: PATCHing payer B with A's email is refused.
    p = await api.patch(f"/payers/{m['payer_b']}", json={"email": "asha@example.com"}, headers=bearer(m["token"]))
    assert p.status_code == 409
    # Invoicing payer B to A's address must not quietly make it B's email either.
    await api.post("/invoices", headers=bearer(m["token"]), json={
        "payer_id": str(m["payer_b"]), "due_date": (date.today() + timedelta(days=3)).isoformat(),
        "amount_paise": 500_00, "email": "asha@example.com"})
    b = db.execute("select email from payers where id = %s", (m["payer_b"],)).fetchone()
    assert b["email"] != "asha@example.com", "payer B now shows payer A's email (identity rule bypassed)"


# ---------------------------------------------------------------------------
# RealPaytm request shapes (respx; no network): link + QR rules, checksum over exact bytes,
# FIXED fallback, `_Q` QR fallback reconciled by webhook, Status API mapping.
# ---------------------------------------------------------------------------
REAL_MID = "HISAABSTAGE00000001"
REAL_KEY = "RealStageKey#016"
CALLBACK = "https://api.hisaab.example"
LINK_URL = "https://securestage.paytmpayments.com/link/create"
QR_URL = "https://securegw-stage.paytmpayments.in/paymentservices/qr/create"
STATUS_URL = "https://securegw-stage.paytmpayments.in/v3/order/status"


def _ok_link(req):
    body = json.loads(req.content)["body"]
    return httpx.Response(200, json={"head": {}, "body": {
        "resultInfo": {"resultStatus": "SUCCESS", "resultCode": "200", "resultMessage": "Success"},
        "linkId": 424242, "shortUrl": "https://paytm.me/x-AbCd", "linkOrderId": body["linkOrderId"]}})


def _ok_qr(req):
    body = json.loads(req.content)["body"]
    return httpx.Response(200, json={"head": {}, "body": {
        "resultInfo": {"resultStatus": "SUCCESS", "resultCode": "QR_0001", "resultMsg": "SUCCESS"},
        "qrCodeId": "QR123", "qrData": f"upi://pay?pa=x@paytm&tr={body['orderId']}",
        "image": qr_png_b64("upi://pay?pa=x@paytm")}})


def _body_str(raw: bytes) -> str:
    s = raw.decode()
    return s[len('{"body":'):s.index(',"head":')]


@pytest.fixture
def real_paytm(monkeypatch, settings):
    from app.services.paytm.real import RealPaytm

    client = RealPaytm("staging", REAL_MID, REAL_KEY, CALLBACK)
    monkeypatch.setattr("app.services.invoices.get_paytm", lambda: client)
    monkeypatch.setattr("app.services.payments.get_paytm", lambda: client)
    return client


async def _create_real(other_merchant, **kw):
    from app.db import connection
    from app.services import invoices as svc

    async with connection() as conn:
        return await svc.create_invoice(conn, other_merchant["merchant_id"], payer_id=other_merchant["payer_a"],
                                        due_date=date.today() + timedelta(days=10),
                                        line_items=[{"name": "Canteen staples", "qty": 2, "unit_price_paise": 1500_00}],
                                        send=False, **kw)


async def test_real_link_and_qr_requests_follow_the_paytm_rules(real_paytm, other_merchant, settings):
    with respx.mock(assert_all_called=False) as mock:
        link = mock.post(LINK_URL).mock(side_effect=_ok_link)
        qr = mock.post(QR_URL).mock(side_effect=_ok_qr)
        mock.route(host="127.0.0.1").pass_through()
        mock.route(host="localhost").pass_through()
        inv = await _create_real(other_merchant)
    assert link.call_count == 1 and qr.call_count == 1
    lreq, qreq = link.calls[0].request, qr.calls[0].request
    for req in (lreq, qreq):
        full = json.loads(req.content)
        assert PaytmChecksum.verifySignature(_body_str(req.content), REAL_KEY, full["head"]["signature"])
    lb = json.loads(lreq.content)["body"]
    assert lb["mid"] == REAL_MID and lb["linkType"] == "INVOICE"
    assert re.fullmatch(r"[A-Za-z0-9]+", lb["linkName"]), lb["linkName"]
    assert 3 <= len(lb["linkDescription"]) <= 30 and re.fullmatch(r"[A-Za-z0-9 ]+", lb["linkDescription"])
    assert lb["amount"] == 3000 and lb["invoiceId"] == inv["number"]
    exp = datetime.strptime(lb["expiryDate"], "%d/%m/%Y").date()
    assert date.today() < exp <= date.today() + timedelta(days=365)
    assert lb["linkOrderId"] == inv["paytm_order_id"]
    assert lb["statusCallbackUrl"] == f"{CALLBACK}/webhooks/paytm/link"
    assert lb["sendSms"] is False and lb["sendEmail"] is False
    assert json.loads(lreq.content)["head"]["tokenType"] == "AES"
    qb = json.loads(qreq.content)["body"]
    assert qb["orderId"] == inv["paytm_order_id"] and qb["amount"] == "3000.00"
    assert qb["businessType"] == "UPI_QR_CODE" and 0 < len(qb["posId"]) <= 50
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", qb["expiryDate"])
    assert inv["link_id"] == "424242" and inv["link_url"] == "https://paytm.me/x-AbCd"
    assert inv["paytm_raw"]["link"]["body"]["resultInfo"]["resultStatus"] == "SUCCESS"


async def test_real_qr_response_image_is_not_stored_in_paytm_raw(real_paytm, other_merchant):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(LINK_URL).mock(side_effect=_ok_link)
        mock.post(QR_URL).mock(side_effect=_ok_qr)
        mock.route(host="127.0.0.1").pass_through()
        inv = await _create_real(other_merchant)
    assert '"image"' not in json.dumps(inv["paytm_raw"]), "base64 QR PNG copied into invoices.paytm_raw"


async def test_real_invoice_link_rejected_falls_back_to_fixed(real_paytm, other_merchant):
    bodies = []

    def link(req):
        bodies.append(json.loads(req.content)["body"])
        if len(bodies) == 1:
            return httpx.Response(200, json={"body": {"resultInfo": {
                "resultStatus": "FAILED", "resultCode": "5011", "resultMessage": "Invoice details required"}}})
        return _ok_link(req)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(LINK_URL).mock(side_effect=link)
        mock.post(QR_URL).mock(side_effect=_ok_qr)
        mock.route(host="127.0.0.1").pass_through()
        inv = await _create_real(other_merchant)
    assert [b["linkType"] for b in bodies] == ["INVOICE", "FIXED"]
    assert bodies[1]["linkOrderId"] == inv["paytm_order_id"] and bodies[1]["amount"] == 3000
    assert inv["paytm_raw"]["link_type"] == "FIXED" and inv["status"] == "sent"


async def test_real_qr_duplicate_order_uses_q_suffix_and_reconciles(real_paytm, other_merchant, db, settings,
                                                                    monkeypatch, api):
    orders = []

    def qr(req):
        body = json.loads(req.content)["body"]
        orders.append(body["orderId"])
        if len(orders) == 1:
            return httpx.Response(200, json={"body": {"resultInfo": {
                "resultStatus": "FAILURE", "resultCode": "QR_1020", "resultMsg": "Duplicate order id"}}})
        return _ok_qr(req)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(LINK_URL).mock(side_effect=_ok_link)
        mock.post(QR_URL).mock(side_effect=qr)
        mock.route(host="127.0.0.1").pass_through()
        inv = await _create_real(other_merchant)
    assert orders == [inv["paytm_order_id"], inv["paytm_order_id"] + "_Q"]
    assert inv["qr_order_id"] == inv["paytm_order_id"] + "_Q"
    # A QR payment reported on the dashboard "Payment Status" webhook reconciles on the _Q order ID.
    monkeypatch.setattr(settings, "paytm_mode", "staging")
    monkeypatch.setattr(settings, "paytm_merchant_key", REAL_KEY)
    f = webhook_fields(settings, inv, ORDERID=inv["qr_order_id"])
    for k in ("LINKDESCRIPTION", "PAYMENTEMAILID", "PAYMENTMOBILENUMBER", "PAYMENTFORM", "CUSTID", "MERC_UNQ_REF",
              "LINKNOTES", "TXNDATETIME"):
        f.pop(k)
    r = await post_webhook(api, signed(f, REAL_KEY), "payment")
    assert r.json()["result"] == "paid"
    [t] = txns_for(db, inv["id"])
    assert t["source"] == "paytm_qr" and t["paytm_order_id"] == inv["qr_order_id"]


async def test_real_status_api_request_and_mapping(real_paytm, other_merchant, db, settings, monkeypatch):
    from app.db import connection
    from app.services.payments import check_payment

    with respx.mock(assert_all_called=False) as mock:
        mock.post(LINK_URL).mock(side_effect=_ok_link)
        mock.post(QR_URL).mock(side_effect=_ok_qr)
        mock.route(host="127.0.0.1").pass_through()
        inv = await _create_real(other_merchant)
    monkeypatch.setattr(settings, "paytm_mode", "staging")
    seen = []

    def status(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"head": {}, "body": {
            "resultInfo": {"resultStatus": "TXN_SUCCESS", "resultCode": "01", "resultMsg": "Txn Success"},
            "txnId": "20261031REALTXN0001", "bankTxnId": "B1", "orderId": inv["paytm_order_id"],
            "txnAmount": "3000.00", "txnType": "SALE", "gatewayName": "PPBLC", "mid": REAL_MID,
            "paymentMode": "UPI", "txnDate": "2026-10-31 10:15:00.0"}})

    with respx.mock(assert_all_called=False) as mock:
        mock.post(STATUS_URL).mock(side_effect=status)
        async with connection() as conn:
            row = await conn.execute("select * from invoices where id = %s", (inv["id"],))
            out = await check_payment(conn, await row.fetchone())
    assert seen[0]["body"] == {"mid": REAL_MID, "orderId": inv["paytm_order_id"]}
    assert out["status"] == "paid"
    [t] = txns_for(db, inv["id"])
    assert t["paytm_txn_id"] == "20261031REALTXN0001" and t["source"] == "paytm_link"
    assert t["paid_at"] == datetime(2026, 10, 31, 10, 15, tzinfo=IST)
