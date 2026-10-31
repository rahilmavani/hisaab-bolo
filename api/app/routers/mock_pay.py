"""Mock Paytm checkout (PAYTM_MODE=mock only). No JWT: the customer opens it from the email."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from ..auth import Conn, verify_token
from ..config import get_settings
from ..db import fetch_one
from ..services.paytm import PaytmError
from ..services.qr import qr_png_b64, upi_pay_uri
from ..services.paytm.mock import send_webhook, simulate_payment

router = APIRouter(prefix="/mock-pay")
_optional_bearer = HTTPBearer(auto_error=False)
OptionalCreds = Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)]


def _require_mock():
    if not get_settings().mock_mode:
        raise HTTPException(404, "Not found")


async def _order(conn, order_id: str) -> dict:
    row = await fetch_one(
        conn,
        """
        select o.order_id, o.status, o.amount_paise, o.link_id, o.txn_id, o.txn_at,
               i.id as invoice_id, i.merchant_id, i.number, i.due_date, i.line_items, i.status as invoice_status,
               p.id as payer_id, p.display_name as payer_name, p.phone as payer_phone, p.email as payer_email,
               m.name as merchant_name
          from paytm_mock_orders o
          left join invoices i on i.paytm_order_id = o.order_id or i.qr_order_id = o.order_id
          left join payers p on p.id = i.payer_id
          left join merchants m on m.id = i.merchant_id
         where o.order_id = %s
        """,
        (order_id,),
    )
    if row is None:
        raise HTTPException(404, "This payment link doesn't exist")
    return row


async def _test_payments_allowed(conn, o: dict, creds: HTTPAuthorizationCredentials | None) -> bool:
    """Simulated payments mark an invoice paid with no money moving. With the shop's real UPI ID set, the
    pay page goes to real customers, so only the signed-in shop owner may simulate (for demos)."""
    if not get_settings().merchant_upi_id:
        return True  # a pure test gateway: nothing on this page is real
    if creds is None:
        return False
    try:
        claims = await verify_token(creds.credentials)
    except HTTPException:
        return False
    owner = await fetch_one(conn, "select 1 from merchants where id = %s and owner_user_id = %s",
                            (o["merchant_id"], claims["sub"]))
    return owner is not None


@router.get("/{order_id}")
async def show(order_id: str, conn: Conn, creds: OptionalCreds):
    _require_mock()
    o = await _order(conn, order_id)
    s = get_settings()
    upi = None
    if s.merchant_upi_id and o["invoice_status"] in ("sent", "overdue"):
        # Real money, straight to the shop's UPI ID: the page offers it beside the test payment.
        payee = s.merchant_upi_name or o["merchant_name"] or "Shop"
        uri = upi_pay_uri(s.merchant_upi_id, payee, o["amount_paise"], f"{o['number']} {o['merchant_name']}")
        upi = {"uri": uri, "qr_png": qr_png_b64(uri), "payee_name": payee, "upi_id": s.merchant_upi_id}
    return {
        "upi": upi,
        "test_payments": await _test_payments_allowed(conn, o, creds),
        "order_id": o["order_id"],
        "status": o["status"],
        "invoice_status": o["invoice_status"],
        "amount_paise": o["amount_paise"],
        "merchant_name": o["merchant_name"],
        "invoice_number": o["number"],
        "payer_name": o["payer_name"],
        "due_date": o["due_date"],
        "line_items": o["line_items"] or [],
        "txn_id": o["txn_id"],
        "paid_at": o["txn_at"],
    }


class PayBody(BaseModel):
    outcome: Literal["success", "failure", "success_no_webhook"] = "success"


@router.post("/{order_id}")
async def pay(order_id: str, body: PayBody, conn: Conn, creds: OptionalCreds):
    """Settle the mock order and deliver Paytm's signed webhook to our real route."""
    _require_mock()
    o = await _order(conn, order_id)
    if not await _test_payments_allowed(conn, o, creds):
        raise HTTPException(403, "Test payments are off for customers: pay with your UPI app instead")
    if o["invoice_status"] == "cancelled":
        raise HTTPException(409, "This invoice was cancelled by the merchant")
    try:
        fields = await simulate_payment(
            order_id,
            body.outcome,
            payer_phone=o["payer_phone"],
            payer_email=o["payer_email"],
            cust_id=f"PTM{str(o['payer_id']).replace('-', '')[:10].upper()}" if o["payer_id"] else None,
            link_description=f"Invoice {o['number']}" if o["number"] else "",
            link_notes=f"invoice_id={o['invoice_id']}" if o["invoice_id"] else "",
        )
    except PaytmError as e:
        raise HTTPException(409 if e.code == "ALREADY_PAID" else 404, e.message) from e
    delivered = None
    if body.outcome != "success_no_webhook":
        delivered = await send_webhook("/webhooks/paytm/link", fields)
    return {
        "status": fields["STATUS"],
        "txn_id": fields["TXNID"],
        "amount": fields["TXNAMOUNT"],
        "webhook_status": delivered,
        "webhook_sent": delivered is not None,
    }
