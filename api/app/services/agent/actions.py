"""Confirming a pending action: the only path from the agent to a write."""

from datetime import date
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ...db import app_now, fetch_one
from .. import invoices as inv_svc
from ..recurring import RecurringError, create_due_invoices, set_recurring


class ActionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def confirm(conn: AsyncConnection, merchant_id: UUID, token: str) -> dict:
    async with conn.transaction():
        action = await fetch_one(
            conn, "select * from pending_actions where token = %s and merchant_id = %s for update", (token, merchant_id)
        )
        if action is None:
            raise ActionError(404, "This action doesn't exist")
        if action["confirmed_at"]:
            raise ActionError(409, "Already confirmed")
        if action["expires_at"] < await app_now(conn):
            raise ActionError(410, "This action expired. Ask again to get a fresh one.")
        await conn.execute("update pending_actions set confirmed_at = app_now() where token = %s", (token,))
    a = action["args"]
    try:
        if action["tool"] == "create_invoice":
            inv = await inv_svc.create_invoice(
                conn, merchant_id, payer_id=UUID(a["payer_id"]), due_date=date.fromisoformat(a["due_date"]),
                line_items=a["line_items"], email=a.get("email"), send=True,
            )
            result = {"kind": "invoice", "invoice": inv,
                      "message": f"Invoice {inv['number']} created" + (
                          " and emailed" if inv["sent_at"]
                          else f", but the email didn't go out: {inv['email_error']}" if inv["email_error"] else "")}
        elif action["tool"] == "send_reminder":
            sent = await inv_svc.remind(conn, merchant_id, UUID(a["invoice_id"]))
            inv = await inv_svc.get_invoice(conn, merchant_id, UUID(a["invoice_id"]))
            result = {"kind": "reminder", "invoice": inv, "message": f"Reminder emailed to {sent['intended_to']}"}
        else:
            schedule = await set_recurring(conn, merchant_id, UUID(a["payer_id"]), cadence=a["cadence"],
                                           amount_paise=a["amount_paise"], email=a.get("email"))
            dues = await create_due_invoices(conn, schedule["id"])
            result = {"kind": "recurring", "schedule": schedule, "dues_created": dues,
                      "message": "Marked recurring" + (" (tracking only, no email)" if schedule["tracking_only"] else "")}
    except (inv_svc.InvoiceError, RecurringError) as e:
        # Let the merchant retry: the action was not carried out.
        await conn.execute("update pending_actions set confirmed_at = null where token = %s", (token,))
        raise ActionError(e.status, str(e)) from e
    await conn.execute("update pending_actions set result = %s where token = %s",
                       (Jsonb({"message": result["message"]}), token))
    return result
