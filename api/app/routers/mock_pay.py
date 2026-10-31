"""Mock Paytm checkout (PAYTM_MODE=mock only). No JWT: the customer opens it from the email."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import Conn
from ..config import get_settings
from ..db import fetch_one
from ..services.paytm import PaytmError
from ..services.paytm.mock import send_webhook, simulate_payment

router = APIRouter(prefix="/mock-pay")


def _require_mock():
    if not get_settings().mock_mode:
        raise HTTPException(404, "Not found")


async def _order(conn, order_id: str) -> dict:
    row = await fetch_one(
        conn,
        """
        select o.order_id, o.status, o.amount_paise, o.link_id, o.txn_id, o.txn_at,
               i.id as invoice_id, i.number, i.due_date, i.line_items, i.status as invoice_status,
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


@router.get("/{order_id}")
async def show(order_id: str, conn: Conn):
    _require_mock()
    o = await _order(conn, order_id)
    return {
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
async def pay(order_id: str, body: PayBody, conn: Conn):
    """Settle the mock order and deliver Paytm's signed webhook to our real route."""
    _require_mock()
    o = await _order(conn, order_id)
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
