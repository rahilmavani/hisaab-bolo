"""Invoices: create (number + order ID + Paytm link + QR + email), remind, check.

One order ID, one link and one QR per invoice. The link's linkOrderId is the
invoice's paytm_order_id, so the webhook's ORDERID matches exactly.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import date, datetime, time, timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ..config import get_settings
from ..db import app_now, app_today, fetch_all, fetch_one
from ..money import IST
from .email import EmailError, render_payment_email, send_email
from .paytm import DuplicateOrderError, LinkRequest, PaytmError, QrRequest, get_paytm
from .payers import PayerError, add_identifier, conflict_message, identifier_owner, normalise_phone, update_payer
from .storage import upload_qr_png

log = logging.getLogger(__name__)

LINK_EXPIRY_AFTER_DUE = timedelta(days=30)


class InvoiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


INVOICE_SELECT = """
    select i.*, p.display_name as payer_name, p.email as payer_email, p.phone as payer_phone,
           (i.due_date - app_today()) as due_in_days,
           (select max(r.sent_at) from reminders r where r.invoice_id = i.id and r.channel = 'email') as last_reminded_at,
           (select count(*) from reminders r where r.invoice_id = i.id)::int as reminder_count,
           lc.sent_at as last_called_at, lc.call_status as last_call_status, lc.call_outcome as last_call_outcome,
           lc.call_promised_date as last_call_promised_date
      from invoices i join payers p on p.id = i.payer_id
      left join lateral (select r.sent_at, r.call_status, r.call_outcome, r.call_promised_date from reminders r
                          where r.invoice_id = i.id and r.channel = 'call' order by r.sent_at desc limit 1) lc on true
"""


async def get_invoice(conn: AsyncConnection, merchant_id: UUID, invoice_id: UUID) -> dict | None:
    return await fetch_one(conn, INVOICE_SELECT + " where i.merchant_id = %s and i.id = %s", (merchant_id, invoice_id))


async def list_invoices(conn: AsyncConnection, merchant_id: UUID, status: str = "all") -> list[dict]:
    where = {
        "open": "and i.status in ('sent', 'overdue', 'draft')",
        "paid": "and i.status = 'paid'",
        "all": "",
    }[status]
    return await fetch_all(
        conn,
        INVOICE_SELECT + f" where i.merchant_id = %s {where} "
        "order by case when i.status in ('overdue', 'sent', 'draft') then 0 else 1 end, i.due_date, i.created_at desc",
        (merchant_id,),
    )


def _alnum(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s)


def normalise_items(items: list[dict], amount_paise: int | None) -> tuple[list[dict], int]:
    clean = []
    for it in items:
        name = str(it.get("name", "")).strip()
        qty = int(it.get("qty", 1))
        unit = int(it.get("unit_price_paise", 0))
        if not name or qty <= 0 or unit <= 0:
            raise InvoiceError(422, "Each line item needs a name, a quantity above 0 and a price above ₹0")
        clean.append({"name": name[:80], "qty": qty, "unit_price_paise": unit})
    total = sum(i["qty"] * i["unit_price_paise"] for i in clean)
    if clean and amount_paise is not None and amount_paise != total:
        raise InvoiceError(422, "The amount doesn't match the line items")
    if not clean:
        if amount_paise is None:
            raise InvoiceError(422, "Give an amount or at least one line item")
        clean = [{"name": "Payment", "qty": 1, "unit_price_paise": amount_paise}]
        total = amount_paise
    if total < 100:
        raise InvoiceError(422, "The amount must be at least ₹1")
    return clean, total


async def create_invoice(
    conn: AsyncConnection,
    merchant_id: UUID,
    *,
    payer_id: UUID,
    due_date: date,
    line_items: list[dict] | None = None,
    amount_paise: int | None = None,
    gst_taxable: bool = False,
    payer_gstin: str | None = None,
    email: str | None = None,
    send: bool = True,
    recurring_id: UUID | None = None,
    grace_days: int = 0,
    allow_past_due: bool = False,
    call_remind: bool = False,
    phone: str | None = None,
) -> dict:
    items, total = normalise_items(line_items or [], amount_paise)
    today = await app_today(conn)
    if due_date < today and not allow_past_due:
        raise InvoiceError(422, "The due date can't be in the past")
    if due_date > today + timedelta(days=330):
        raise InvoiceError(422, "The due date must be within 330 days (Paytm links expire within a year)")
    payer = await fetch_one(
        conn, "select * from payers where id = %s and merchant_id = %s and merged_into is null", (payer_id, merchant_id)
    )
    if payer is None:
        raise InvoiceError(404, "Customer not found")
    merchant = await fetch_one(conn, "select name from merchants where id = %s", (merchant_id,))
    # Only the email given here is used: no email means a link-only invoice (never emailed or
    # auto-reminded), even when the customer has an email on file.
    email = (email or "").strip().lower() or None
    if email and (owner := await identifier_owner(conn, merchant_id, "email", email, payer_id)):
        raise InvoiceError(409, conflict_message(email, owner))
    gstin = (payer_gstin or payer["gstin"] or None) if gst_taxable else None
    try:
        phone = normalise_phone(phone) if phone and phone.strip() else None
    except PayerError as e:
        raise InvoiceError(e.status, str(e)) from e
    if phone and phone != payer["phone"] and (owner := await identifier_owner(conn, merchant_id, "phone", phone, payer_id)):
        raise InvoiceError(409, conflict_message(phone, owner))
    # A recurring due follows its schedule (checked when it was set up); the call step skips no-phone ones.
    if call_remind and not recurring_id and not (phone or payer["phone"]):
        raise InvoiceError(422, f"Add a phone number for {payer['display_name']} to call them about this invoice")

    # All or nothing up to the payment link: if Paytm fails, no invoice (or used-up number) is left.
    async with conn.transaction():
        if phone and phone != payer["phone"]:  # the number typed here becomes the customer's number
            await update_payer(conn, merchant_id, payer_id, {"phone": phone})
            payer = {**payer, "phone": phone}
        if email and not payer["email"]:
            await conn.execute("update payers set email = %s where id = %s", (email, payer_id))
        if email:
            await add_identifier(conn, merchant_id, payer_id, "email", email)
        if gst_taxable and payer_gstin and not payer["gstin"]:
            await conn.execute("update payers set gstin = %s where id = %s", (payer_gstin, payer_id))
        number = (await fetch_one(conn, "select next_invoice_number(%s) as n", (merchant_id,)))["n"]
        order_id = f"HB_{_alnum(number)}_{secrets.token_hex(4).upper()}"
        inv = await fetch_one(
            conn,
            """
            insert into invoices (merchant_id, number, payer_id, amount_paise, line_items, gst_taxable, payer_gstin,
                                  due_date, grace_days, status, email_to, paytm_order_id, recurring_id, call_remind,
                                  created_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s, %s, app_now())
            returning *
            """,
            (merchant_id, number, payer_id, total, Jsonb(items), gst_taxable, gstin, due_date, grace_days,
             email, order_id, recurring_id, call_remind),
        )
        assert inv
        await attach_payment_artifacts(conn, inv, merchant["name"], payer)
    # The invoice and its link exist now; a failed email is reported, not undone (retrying the
    # create would bill twice). The merchant can resend it from the invoice page.
    email_error = None
    if send and email:
        try:
            await send_invoice_email(conn, merchant_id, inv["id"], kind="invoice")
        except InvoiceError as e:
            email_error = str(e)
    result = await get_invoice(conn, merchant_id, inv["id"])
    assert result
    return {**result, "email_error": email_error}


async def attach_payment_artifacts(conn: AsyncConnection, inv: dict, merchant_name: str, payer: dict) -> None:
    """Create the Paytm link + dynamic QR for an invoice, store the QR PNG, mark it sent."""
    paytm = get_paytm()
    # A due billed late (catch-up after downtime) still gets a link that hasn't expired.
    expiry = max(inv["due_date"], await app_today(conn)) + LINK_EXPIRY_AFTER_DUE
    try:
        link = await paytm.create_link(LinkRequest(
            order_id=inv["paytm_order_id"],
            amount_paise=inv["amount_paise"],
            invoice_number=inv["number"],
            link_name=_alnum(merchant_name + inv["number"])[:64],
            description=f"Invoice {_alnum(inv['number'])}",
            expiry=expiry,
            payer_name=payer["display_name"],
            payer_email=payer.get("email"),
            payer_phone=payer.get("phone"),
            notes=f"invoice_id={inv['id']}",
        ))
        qr_req = QrRequest(order_id=inv["paytm_order_id"], amount_paise=inv["amount_paise"],
                           display_name=merchant_name, invoice_number=inv["number"],
                           expiry=datetime.combine(expiry, time(23, 59, 59)))
        try:
            qr = await paytm.create_qr(qr_req)
        except DuplicateOrderError:
            qr_req.order_id = f"{inv['paytm_order_id']}_Q"
            qr = await paytm.create_qr(qr_req)
    except PaytmError as e:
        log.error("paytm artifacts failed for %s: %s", inv["number"], e)
        raise InvoiceError(502, f"Paytm couldn't create the payment link: {e.message} ({e.code})") from e

    try:
        qr_url = await upload_qr_png(f"{inv['merchant_id']}/{qr.order_id}.png", qr.image_b64)
    except Exception as e:
        log.error("QR upload failed for %s: %s", inv["number"], e)
        raise InvoiceError(502, "Couldn't store the payment QR image. Try again in a minute.") from e
    await conn.execute(
        """
        update invoices set link_id = %s, link_url = %s, qr_image_url = %s,
               qr_order_id = case when %s <> paytm_order_id then %s end,
               paytm_raw = paytm_raw || %s,
               status = case when status = 'draft' then 'sent' else status end
         where id = %s
        """,
        (link.link_id, link.short_url, qr_url, qr.order_id, qr.order_id,
         Jsonb({"link": link.raw, "link_type": link.link_type,
                "qr": {k: v for k, v in qr.raw.items() if k != "image"}, "qr_data": qr.qr_data}),
         inv["id"]),
    )


async def send_invoice_email(conn: AsyncConnection, merchant_id: UUID, invoice_id: UUID, kind: str,
                             during_call: UUID | None = None) -> dict:
    inv = await get_invoice(conn, merchant_id, invoice_id)
    if inv is None:
        raise InvoiceError(404, "Invoice not found")
    if inv["status"] in ("paid", "cancelled"):
        raise InvoiceError(409, f"Invoice {inv['number']} is already {inv['status']}")
    to = inv["email_to"] or inv["payer_email"]
    if not to:
        raise InvoiceError(422, f"{inv['payer_name']} has no email address. Add one to send reminders.")
    if not inv["link_url"]:
        raise InvoiceError(409, "This invoice has no payment link yet")
    merchant = await fetch_one(conn, "select name from merchants where id = %s", (merchant_id,))
    qr_b64 = None
    if inv["qr_image_url"]:
        from .storage import is_public_url

        if not is_public_url(inv["qr_image_url"]):
            qr_b64 = await _fetch_qr_b64(inv["qr_image_url"])
    today = await app_today(conn)
    s = get_settings()
    subject, body, attachments = render_payment_email(
        upi_payee=(f"{s.merchant_upi_name or merchant['name']} · {s.merchant_upi_id}"
                   if s.mock_mode and s.merchant_upi_id else None),
        kind=kind,
        merchant_name=merchant["name"],
        payer_name=inv["payer_name"],
        intended_to=to,
        number=inv["number"],
        amount_paise=inv["amount_paise"],
        issued_on=inv["created_at"].astimezone(IST).date(),
        due_date=inv["due_date"],
        pay_url=inv["link_url"],
        qr_url=inv["qr_image_url"],
        qr_b64=qr_b64,
        line_items=inv["line_items"],
        gst_taxable=inv["gst_taxable"],
        payer_gstin=inv["payer_gstin"],
        days_overdue=(today - inv["due_date"]).days,
    )
    try:
        sent = await send_email(to, subject, body, attachments)
    except EmailError as e:
        raise InvoiceError(502, str(e)) from e
    now = await app_now(conn)
    reminder = await fetch_one(
        conn,
        "insert into reminders (merchant_id, invoice_id, kind, to_email, sent_at, resend_id, sent_during_call) "
        "values (%s, %s, %s, %s, %s, %s, %s) returning *",
        (merchant_id, invoice_id, kind, to, now, sent.id, during_call),
    )
    # sent_at: when the customer first got the link by email (every email carries it). Any overdue
    # reminder also satisfies the queued automatic one.
    await conn.execute(
        "update invoices set sent_at = coalesce(sent_at, %s), "
        "overdue_reminder_pending = overdue_reminder_pending and %s = 'invoice' where id = %s",
        (now, kind, invoice_id),
    )
    return {"reminder": reminder, "delivered_to": sent.delivered_to, "intended_to": sent.intended_to,
            "subject": subject}


async def _fetch_qr_b64(url: str) -> str | None:
    import base64

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        r.raise_for_status()
        return base64.b64encode(r.content).decode()
    except httpx.HTTPError as e:
        log.warning("could not fetch QR %s: %s", url, e)
        return None


async def mark_paid(conn: AsyncConnection, merchant_id: UUID, invoice_id: UUID) -> dict:
    """The money reached the shop outside Paytm (straight to its UPI ID, or cash): record the payment
    and close the invoice. Paytm sends no notification for those, so the merchant confirms them."""
    async with conn.transaction():
        inv = await fetch_one(
            conn, "select * from invoices where id = %s and merchant_id = %s for update", (invoice_id, merchant_id)
        )
        if inv is None:
            raise InvoiceError(404, "Invoice not found")
        if inv["status"] not in ("sent", "overdue"):
            raise InvoiceError(409, f"Invoice {inv['number']} is already {inv['status']}")
        now = await app_now(conn)
        await conn.execute(
            "insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source, payment_mode, "
            "description, invoice_id) values (%s, %s, %s, %s, 'manual', 'UPI', %s, %s)",
            (merchant_id, inv["payer_id"], inv["amount_paise"], now, f"Invoice {inv['number']}", invoice_id),
        )
        await conn.execute(
            "update invoices set status = 'paid', paid_at = %s, overdue_reminder_pending = false where id = %s",
            (now, invoice_id),
        )
    result = await get_invoice(conn, merchant_id, invoice_id)
    assert result
    return result


async def remind(conn: AsyncConnection, merchant_id: UUID, invoice_id: UUID, during_call: UUID | None = None) -> dict:
    inv = await get_invoice(conn, merchant_id, invoice_id)
    if inv is None:
        raise InvoiceError(404, "Invoice not found")
    kind = "overdue" if inv["status"] == "overdue" or inv["due_in_days"] < 0 else "invoice"
    return await send_invoice_email(conn, merchant_id, invoice_id, kind=kind, during_call=during_call)


async def cancel(conn: AsyncConnection, merchant_id: UUID, invoice_id: UUID) -> dict:
    if await get_invoice(conn, merchant_id, invoice_id) is None:
        raise InvoiceError(404, "Invoice not found")
    row = await fetch_one(
        conn,
        "update invoices set status = 'cancelled' where id = %s and merchant_id = %s and status <> 'paid' returning id",
        (invoice_id, merchant_id),
    )
    if row is None:
        raise InvoiceError(409, "Only unpaid invoices can be cancelled")
    inv = await get_invoice(conn, merchant_id, invoice_id)
    assert inv
    return inv
