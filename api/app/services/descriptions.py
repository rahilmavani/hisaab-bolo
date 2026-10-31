"""AI payment descriptions: Haiku writes up to 8 words plus a category.

Inputs: amount, time, link/invoice notes and that payer's last 5 descriptions.
Merchant edits win: rows with description_by = 'merchant' are never touched.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

import anthropic
from psycopg import AsyncConnection

from ..config import get_settings
from ..db import connection, fetch_all, fetch_one
from ..money import IST, format_inr

log = logging.getLogger(__name__)

CATEGORIES = ["Dairy", "Groceries", "Staples", "Snacks", "Household", "Bulk order", "Services", "Other"]

SYSTEM = (
    "You label payments received by an Indian kirana (grocery) store so the owner can scan their ledger. "
    "Write a plain description of what the payment was most likely for, at most 8 words, no trailing full stop, "
    "no amounts, no customer name. Prefer the store's existing wording for this customer when it fits. "
    "If the payment settles an invoice, say what the invoice was for. Pick exactly one category."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
    },
    "required": ["description", "category"],
    "additionalProperties": False,
}

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key or None, max_retries=2)
    return _client


async def _context(conn: AsyncConnection, txn_id: UUID) -> dict | None:
    t = await fetch_one(
        conn,
        """
        select t.id, t.merchant_id, t.payer_id, t.amount_paise, t.paid_at, t.payment_mode, t.description_by,
               p.display_name as payer_name, i.number as invoice_number, i.line_items, t.raw->>'LINKDESCRIPTION' as link_desc
          from transactions t
          left join payers p on p.id = t.payer_id
          left join invoices i on i.id = t.invoice_id
         where t.id = %s
        """,
        (txn_id,),
    )
    if t is None or t["description_by"] == "merchant":
        return None
    t["history"] = [
        r["description"] for r in await fetch_all(
            conn,
            "select description from transactions where payer_id = %s and id <> %s and description is not null "
            "order by paid_at desc limit 5",
            (t["payer_id"], txn_id),
        )
    ] if t["payer_id"] else []
    return t


def _prompt(t: dict) -> str:
    when = t["paid_at"].astimezone(IST)
    lines = [
        f"Amount: {format_inr(t['amount_paise'])}",
        f"Paid at: {when.strftime('%A %d %b, %I:%M %p')} IST",
        f"Payment mode: {t['payment_mode'] or 'UPI'}",
    ]
    if t["invoice_number"]:
        items = ", ".join(f"{i.get('name')} x{i.get('qty', 1)}" for i in (t["line_items"] or []))
        lines.append(f"Settles invoice {t['invoice_number']}: {items}")
    if t["link_desc"]:
        lines.append(f"Payment link note: {t['link_desc']}")
    lines.append("This customer's recent descriptions: " + ("; ".join(t["history"]) if t["history"] else "none"))
    return "\n".join(lines)


async def generate(t: dict) -> dict | None:
    s = get_settings()
    if not s.anthropic_api_key or not s.ai_descriptions:
        return None
    try:
        resp = await client().messages.create(
            model=get_settings().agent_model,
            max_tokens=200,
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": _prompt(t)}],
        )
    except anthropic.APIError as e:
        log.warning("description generation failed: %s", e)
        return None
    if resp.stop_reason == "refusal":
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        return None
    words = out["description"].strip().rstrip(".").split()
    return {"description": " ".join(words[:8]), "category": out["category"]}


async def describe(conn: AsyncConnection, txn_id: UUID) -> dict | None:
    t = await _context(conn, txn_id)
    if t is None:
        return None
    out = await generate(t)
    if out is None:
        return None
    # Only fill if still empty and not edited by the merchant in the meantime.
    row = await fetch_one(
        conn,
        "update transactions set description = %s, category = %s, description_by = 'ai' "
        "where id = %s and description is null and description_by is distinct from 'merchant' returning id",
        (out["description"], out["category"], txn_id),
    )
    return out if row else None


async def describe_pending(conn: AsyncConnection, limit: int = 20) -> int:
    rows = await fetch_all(
        conn,
        "select id from transactions where description is null and description_by is distinct from 'merchant' "
        "order by paid_at desc limit %s",
        (limit,),
    )
    done = 0
    for r in rows:
        if await describe(conn, r["id"]):
            done += 1
    return done


async def describe_transaction_later(txn_id: UUID) -> None:
    """Background task after a webhook: describe the new payment right away."""
    try:
        async with connection() as conn:
            await describe(conn, txn_id)
    except Exception:
        log.exception("background description failed for %s", txn_id)
