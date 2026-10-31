"""Paying the shop's own UPI ID (mock mode): invoice QR, the pay page's UPI button, email copy, Mark as paid."""

from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from app.services import email as email_svc
from app.services.qr import upi_pay_uri

from .conftest import bearer
from .test_invoices_paytm import make_invoice

UPI_ID = "shopkeeper@pthdfc"


@pytest.fixture
def shop_upi(settings, monkeypatch):
    monkeypatch.setattr(settings, "merchant_upi_id", UPI_ID)
    monkeypatch.setattr(settings, "merchant_upi_name", "ASHA STORES")


def params(uri: str) -> dict:
    assert uri.startswith("upi://pay?")
    return {k: v[0] for k, v in parse_qs(urlparse(uri).query).items()}


def test_upi_pay_uri_fills_the_amount_without_merchant_only_fields():
    uri = upi_pay_uri(UPI_ID, "ASHA STORES", 245000, "INV-0415 Asha Stores")
    assert params(uri) == {"pa": UPI_ID, "pn": "ASHA STORES", "am": "2450.00", "cu": "INR", "tn": "INV-0415 Asha Stores"}
    assert "@" in uri and "%40" not in uri  # the UPI ID is readable, as apps expect
    assert "tr=" not in uri and "mc=" not in uri


def test_upi_note_is_capped_for_apps_that_truncate():
    assert len(params(upi_pay_uri(UPI_ID, "X", 100, "n" * 80))["tn"]) == 50


async def test_invoice_qr_pays_the_shops_upi_id_with_the_amount(api, other_merchant, shop_upi):
    inv = await make_invoice(api, other_merchant)
    p = params(inv["paytm_raw"]["qr_data"])
    assert p["pa"] == UPI_ID and p["pn"] == "ASHA STORES" and p["am"] == "3000.00" and inv["number"] in p["tn"]


async def test_without_a_upi_id_the_qr_stays_the_mock_gateways(api, other_merchant):
    inv = await make_invoice(api, other_merchant)
    assert params(inv["paytm_raw"]["qr_data"])["pa"] == "mock@paytm"


async def test_pay_page_offers_the_upi_app_for_an_open_invoice(api, other_merchant, shop_upi):
    inv = await make_invoice(api, other_merchant)
    page = (await api.get(f"/mock-pay/{inv['paytm_order_id']}")).json()
    upi = page["upi"]
    assert upi["upi_id"] == UPI_ID and upi["payee_name"] == "ASHA STORES" and upi["qr_png"]
    assert params(upi["uri"])["am"] == "3000.00"


async def test_pay_page_has_no_upi_button_once_paid_or_without_a_upi_id(api, other_merchant, shop_upi, settings,
                                                                       monkeypatch):
    inv = await make_invoice(api, other_merchant)
    await api.post(f"/invoices/{inv['id']}/mark-paid", headers=bearer(other_merchant["token"]))
    assert (await api.get(f"/mock-pay/{inv['paytm_order_id']}")).json()["upi"] is None
    monkeypatch.setattr(settings, "merchant_upi_id", "")
    other = await make_invoice(api, other_merchant)
    assert (await api.get(f"/mock-pay/{other['paytm_order_id']}")).json()["upi"] is None


async def test_invoice_email_says_any_upi_app_and_who_gets_paid(api, other_merchant, shop_upi):
    email_svc.SENT.clear()
    await make_invoice(api, other_merchant)
    html = email_svc.SENT[-1]["html"]
    assert "with any UPI app" in html and f"pays ASHA STORES · {UPI_ID}" in html and "with Paytm" not in html


def test_email_without_a_upi_id_keeps_the_paytm_wording():
    _, html, _ = email_svc.render_payment_email(
        kind="invoice", merchant_name="Shop", payer_name="Asha", intended_to="a@example.com", number="INV-1",
        amount_paise=10000, issued_on=date(2026, 10, 31), due_date=date(2026, 11, 7), pay_url="https://x/pay",
        qr_url=None, qr_b64=None, line_items=[], gst_taxable=False, payer_gstin=None)
    assert "with Paytm" in html and "pays " not in html


async def test_mark_paid_records_the_payment_and_closes_the_invoice(api, other_merchant, db):
    inv = await make_invoice(api, other_merchant)
    r = await api.post(f"/invoices/{inv['id']}/mark-paid", headers=bearer(other_merchant["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paid"
    t = db.execute("select source, amount_paise, payment_mode, payer_id from transactions where invoice_id = %s",
                   (inv["id"],)).fetchall()
    assert len(t) == 1 and t[0]["source"] == "manual" and t[0]["amount_paise"] == 3000_00
    assert str(t[0]["payer_id"]) == str(other_merchant["payer_a"]) and t[0]["payment_mode"] == "UPI"
    again = await api.post(f"/invoices/{inv['id']}/mark-paid", headers=bearer(other_merchant["token"]))
    assert again.status_code == 409  # one payment per invoice, however often it's tapped


async def test_mark_paid_refuses_cancelled_invoices_and_other_merchants(api, other_merchant, demo_token):
    inv = await make_invoice(api, other_merchant)
    assert (await api.post(f"/invoices/{inv['id']}/mark-paid", headers=bearer(demo_token))).status_code == 404
    assert (await api.post(f"/invoices/{inv['id']}/mark-paid")).status_code == 401
    await api.post(f"/invoices/{inv['id']}/cancel", headers=bearer(other_merchant["token"]))
    r = await api.post(f"/invoices/{inv['id']}/mark-paid", headers=bearer(other_merchant["token"]))
    assert r.status_code == 409 and "cancelled" in r.json()["detail"]


async def test_customers_cannot_simulate_a_payment_when_the_shop_has_a_real_upi_id(api, other_merchant, db, shop_upi):
    """A simulated payment marks the invoice paid with no money moving; on a page real customers open,
    only the signed-in shop owner may use it."""
    inv = await make_invoice(api, other_merchant)
    order = inv["paytm_order_id"]
    assert (await api.get(f"/mock-pay/{order}")).json()["test_payments"] is False
    for headers in ({}, bearer("not.a.token")):
        r = await api.post(f"/mock-pay/{order}", json={"outcome": "success"}, headers=headers)
        assert r.status_code == 403, r.text
    assert db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()["status"] == "sent"
    owner = bearer(other_merchant["token"])
    assert (await api.get(f"/mock-pay/{order}", headers=owner)).json()["test_payments"] is True
    assert (await api.post(f"/mock-pay/{order}", json={"outcome": "success"}, headers=owner)).status_code == 200


async def test_without_a_upi_id_the_test_gateway_stays_open_to_anyone(api, other_merchant):
    inv = await make_invoice(api, other_merchant)
    page = (await api.get(f"/mock-pay/{inv['paytm_order_id']}")).json()
    assert page["upi"] is None and page["test_payments"] is True
