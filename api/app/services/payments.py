"""Paytm notifications → paid invoices.

Webhooks (link callback and dashboard payment-status) and Status-API results all
go through `record_notification`: idempotent on (provider, TXNID), exact match on
the order ID, amount check, then one DB transaction marks the invoice paid,
inserts the payment and attaches the payer's identifiers.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ..config import get_settings
from ..db import app_now, fetch_all, fetch_one
from ..money import IST, rupees_to_paise
from .paytm import get_paytm
from .payers import add_identifier

log = logging.getLogger(__name__)
PROVIDER = "paytm"


def parse_paytm_time(fields: dict[str, str]) -> datetime | None:
    for key, fmts in (("TXNDATETIME", ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")),
                      ("TXNDATE", ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"))):
        value = (fields.get(key) or "").strip()
        for fmt in fmts:
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=IST)
            except ValueError:
                continue
    return None


async def find_invoice_for(conn: AsyncConnection, fields: dict[str, str]) -> dict | None:
    order_id = fields.get("ORDERID") or ""
    link_id = fields.get("MERC_UNQ_REF") or ""
    return await fetch_one(
        conn,
        """
        select * from invoices
         where paytm_order_id = %(o)s or qr_order_id = %(o)s or (%(l)s <> '' and link_id = %(l)s)
         order by (paytm_order_id = %(o)s) desc
         limit 1
        """,
        {"o": order_id, "l": link_id},
    )


async def record_notification(conn: AsyncConnection, fields: dict[str, str], route: str) -> dict:
    """Process one verified Paytm notification. Safe to call any number of times.

    Everything, including the dedupe row, is one DB transaction: if any step fails, nothing is
    remembered, so Paytm's retry (or the next status check) processes the payment again."""
    async with conn.transaction():
        return await _record(conn, fields, route)


async def _record(conn: AsyncConnection, fields: dict[str, str], route: str) -> dict:
    txn_id = (fields.get("TXNID") or "").strip()
    if not txn_id:
        return {"result": "ignored", "reason": "missing TXNID"}
    payload = {k: v for k, v in fields.items() if k != "CHECKSUMHASH"}
    event = await fetch_one(
        conn,
        "insert into webhook_events (provider, external_id, route, payload) values (%s, %s, %s, %s) "
        "on conflict (provider, external_id) do nothing returning id",
        (PROVIDER, txn_id, route, Jsonb(payload)),
    )
    if event is None:
        return {"result": "duplicate", "txn_id": txn_id}

    async def finish(result: dict, error: str | None = None, merchant_id: UUID | None = None) -> dict:
        await conn.execute(
            "update webhook_events set processed_at = now(), error = %s, merchant_id = coalesce(%s, merchant_id) "
            "where id = %s",
            (error, merchant_id, event["id"]),
        )
        if error:
            log.warning("paytm notification %s: %s", txn_id, error)
        return result

    invoice = await find_invoice_for(conn, fields)
    if invoice is None:
        return await finish({"result": "unmatched", "txn_id": txn_id}, "no invoice for ORDERID/MERC_UNQ_REF")

    if fields.get("STATUS") != "TXN_SUCCESS":
        await conn.execute(
            "update invoices set paytm_raw = paytm_raw || %s where id = %s",
            (Jsonb({"last_failure": {k: payload.get(k) for k in ("TXNID", "STATUS", "RESPCODE", "RESPMSG", "TXNDATE")}}),
             invoice["id"]),
        )
        return await finish({"result": "failure_recorded", "txn_id": txn_id, "invoice_id": invoice["id"]},
                            merchant_id=invoice["merchant_id"])

    try:
        paid_paise = rupees_to_paise(Decimal(fields.get("TXNAMOUNT", "")))
    except (InvalidOperation, ValueError):
        paid_paise = -1
    if paid_paise != invoice["amount_paise"]:
        return await finish({"result": "amount_mismatch", "txn_id": txn_id, "invoice_id": invoice["id"]},
                            f"TXNAMOUNT {fields.get('TXNAMOUNT')} != invoice {invoice['amount_paise']} paise",
                            invoice["merchant_id"])

    paid_at = parse_paytm_time(fields) or await app_now(conn)
    s = get_settings()
    order_id = fields.get("ORDERID") or ""
    # Link payments carry the link ID (MERC_UNQ_REF); dashboard payment-status webhooks (QR) don't.
    is_qr = (order_id and order_id == invoice["qr_order_id"]) or (route == "payment" and not fields.get("MERC_UNQ_REF"))
    source = "mock" if s.mock_mode else ("paytm_qr" if is_qr else "paytm_link")
    async with conn.transaction():
        locked = await fetch_one(conn, "select status, paid_at from invoices where id = %s for update", (invoice["id"],))
        if locked["status"] == "paid":
            return await finish({"result": "already_paid", "txn_id": txn_id, "invoice_id": invoice["id"]},
                                "invoice already paid by another transaction", invoice["merchant_id"])
        await conn.execute("update invoices set status = 'paid', paid_at = %s where id = %s", (paid_at, invoice["id"]))
        txn = await fetch_one(
            conn,
            """
            insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source, paytm_order_id,
                                      paytm_txn_id, payment_mode, gst_taxable, invoice_id, raw)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (paytm_txn_id) where paytm_txn_id is not null do nothing
            returning id
            """,
            (invoice["merchant_id"], invoice["payer_id"], paid_paise, paid_at, source, order_id or None, txn_id,
             fields.get("PAYMENTMODE"), invoice["gst_taxable"], invoice["id"], Jsonb(payload)),
        )
        added = []
        for kind, key in (("phone", "PAYMENTMOBILENUMBER"), ("email", "PAYMENTEMAILID"), ("paytm_cust_id", "CUSTID")):
            if await add_identifier(conn, invoice["merchant_id"], invoice["payer_id"], kind, fields.get(key)):
                added.append(kind)
    return await finish({"result": "paid", "txn_id": txn_id, "invoice_id": invoice["id"],
                         "transaction_id": txn["id"] if txn else None, "identifiers_added": added},
                        merchant_id=invoice["merchant_id"])


async def check_payment(conn: AsyncConnection, invoice: dict) -> dict:
    """Ask Paytm's Transaction Status API about the invoice's order(s); settle if paid."""
    if invoice["status"] == "paid":
        return {"status": "paid", "result": "already_paid"}
    paytm = get_paytm()
    last = None
    for order_id in filter(None, (invoice["paytm_order_id"], invoice["qr_order_id"])):
        st = await paytm.order_status(order_id)
        last = st
        if st.status == "TXN_SUCCESS" and st.txn_id:
            fields = {
                "ORDERID": order_id, "TXNID": st.txn_id, "TXNAMOUNT": st.txn_amount or "",
                "STATUS": "TXN_SUCCESS", "PAYMENTMODE": st.payment_mode or "", "TXNDATE": st.txn_date or "",
                "BANKTXNID": st.bank_txn_id or "", "MID": get_settings().paytm_mid_active,
            }
            outcome = await record_notification(conn, fields, "status_api")
            now = await fetch_one(conn, "select status from invoices where id = %s", (invoice["id"],))
            if now and now["status"] == "paid":
                return {**outcome, "status": "paid"}
            # Paytm has a payment for this order that we could not apply (e.g. a different amount).
            event = await fetch_one(conn, "select error from webhook_events where provider = %s and external_id = %s",
                                    (PROVIDER, st.txn_id))
            return {**outcome, "status": "needs_attention",
                    "reason": (event or {}).get("error") or outcome.get("result")}
    return {"status": (last.status.lower() if last else "pending"), "result": "not_paid"}


async def poll_open_invoices(conn: AsyncConnection, older_than_minutes: int = 2, limit: int = 100) -> list[dict]:
    """Cron fallback when the webhook never arrives. A cancelled invoice's link stays payable until
    it expires, so it is still checked: money paid before (or despite) the cancel lands in the ledger."""
    rows = await fetch_all(
        conn,
        """
        select * from invoices
         where (status in ('sent', 'overdue')
                or (status = 'cancelled' and greatest(due_date, created_at::date) + 30 >= app_today()))
           and link_id is not null
           -- give the webhook a couple of minutes first (a demo-clock reset can put created_at ahead of now)
           and created_at not between app_now() - %s * interval '1 minute' and app_now()
         order by created_at desc limit %s
        """,
        (older_than_minutes, limit),
    )
    results = []
    for inv in rows:
        try:
            out = await check_payment(conn, inv)
        except Exception as e:  # one bad order must not stop the sweep
            log.exception("status poll failed for %s", inv["number"])
            out = {"status": "error", "error": str(e)}
        if out.get("status") == "paid":
            results.append({"invoice": inv["number"], **out})
    return results
