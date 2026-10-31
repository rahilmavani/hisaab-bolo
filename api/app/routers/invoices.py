from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field

from ..auth import Conn, CurrentMerchant
from ..db import fetch_all, fetch_one
from ..services import invoices as svc
from ..services.payments import check_payment

router = APIRouter(prefix="/invoices")


def _raise(e: svc.InvoiceError):
    raise HTTPException(e.status, str(e))


@router.get("")
async def list_invoices(merchant: CurrentMerchant, conn: Conn, status: Literal["open", "paid", "all"] = "all"):
    rows = await svc.list_invoices(conn, merchant.id, status)
    open_rows = [r for r in rows if r["status"] in ("sent", "overdue")]
    return {
        "invoices": rows,
        "outstanding_paise": sum(r["amount_paise"] for r in open_rows),
        "outstanding_customers": len({r["payer_id"] for r in open_rows}),
    }


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: UUID, merchant: CurrentMerchant, conn: Conn):
    inv = await svc.get_invoice(conn, merchant.id, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    inv["reminders"] = await fetch_all(
        conn, "select kind, to_email, sent_at from reminders where invoice_id = %s order by sent_at", (invoice_id,)
    )
    inv["payment"] = await fetch_one(
        conn,
        "select id, amount_paise, paid_at, paytm_txn_id, payment_mode, source from transactions "
        "where invoice_id = %s order by paid_at limit 1",
        (invoice_id,),
    )
    inv["merchant_name"] = merchant.name
    return inv


class LineItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    qty: int = Field(1, ge=1, le=10_000)
    unit_price_paise: int = Field(ge=1)


class InvoiceCreate(BaseModel):
    payer_id: UUID
    due_date: date
    line_items: list[LineItem] = Field(default_factory=list, max_length=50)
    amount_paise: int | None = Field(None, ge=100)
    gst_taxable: bool = False
    payer_gstin: str | None = Field(None, pattern=r"^[0-9]{2}[A-Z0-9]{13}$")
    email: EmailStr | None = None
    send_email: bool = True


@router.post("", status_code=201)
async def create_invoice(body: InvoiceCreate, merchant: CurrentMerchant, conn: Conn):
    try:
        return await svc.create_invoice(
            conn, merchant.id,
            payer_id=body.payer_id, due_date=body.due_date,
            line_items=[i.model_dump() for i in body.line_items], amount_paise=body.amount_paise,
            gst_taxable=body.gst_taxable, payer_gstin=body.payer_gstin, email=body.email, send=body.send_email,
        )
    except svc.InvoiceError as e:
        _raise(e)


@router.post("/{invoice_id}/remind")
async def remind(invoice_id: UUID, merchant: CurrentMerchant, conn: Conn):
    try:
        return await svc.remind(conn, merchant.id, invoice_id)
    except svc.InvoiceError as e:
        _raise(e)


@router.post("/{invoice_id}/check")
async def check(invoice_id: UUID, merchant: CurrentMerchant, conn: Conn):
    inv = await svc.get_invoice(conn, merchant.id, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    outcome = await check_payment(conn, inv)
    return {**outcome, "invoice": await svc.get_invoice(conn, merchant.id, invoice_id)}


@router.post("/{invoice_id}/cancel")
async def cancel(invoice_id: UUID, merchant: CurrentMerchant, conn: Conn):
    try:
        return await svc.cancel(conn, merchant.id, invoice_id)
    except svc.InvoiceError as e:
        _raise(e)
