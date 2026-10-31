from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field

from ..auth import Conn, CurrentMerchant
from ..db import fetch_one
from ..services import ledger
from ..services.recurring import (
    RecurringError,
    create_due_invoices,
    send_unsent_due_emails,
    set_recurring,
    stop_recurring,
)

router = APIRouter()


class RecurringBody(BaseModel):
    cadence: Literal["weekly", "monthly"]
    amount_paise: int = Field(ge=100)
    anchor_date: date | None = None
    grace_days: int = Field(3, ge=0, le=60)
    email: EmailStr | None = None
    auto_remind: bool = True


@router.post("/payers/{payer_id}/recurring", status_code=201)
async def mark_recurring(payer_id: UUID, body: RecurringBody, merchant: CurrentMerchant, conn: Conn):
    try:
        schedule = await set_recurring(conn, merchant.id, payer_id, **body.model_dump())
    except RecurringError as e:
        raise HTTPException(e.status, str(e)) from e
    # The first due appears straight away when it falls inside the lead window.
    errors: list[str] = []
    created = await create_due_invoices(conn, schedule["id"], errors)
    # An upcoming due it adopted that was never emailed (tracked only, or a failed send) goes out now.
    emailed_now = (await send_unsent_due_emails(conn, payer_id))["sent"]
    current = await fetch_one(conn, "select * from recurring_schedules where id = %s", (schedule["id"],))
    already = None
    if not created and not errors and not schedule["updated"]:  # stopped and re-marked: billed before
        already = await fetch_one(
            conn,
            "select number as invoice, due_date from invoices where payer_id = %s and recurring_id is not null "
            "and due_date = %s and status <> 'cancelled'",
            (payer_id, schedule["anchor_date"]),
        )
    return {"schedule": {**current, "tracking_only": schedule["tracking_only"], "updated": schedule["updated"]},
            "dues_created": created, "already_invoiced": already, "due_error": errors[0] if errors else None,
            "emailed_now": emailed_now,
            "payer": await ledger.payer_profile(conn, merchant.id, payer_id)}


@router.delete("/payers/{payer_id}/recurring")
async def unmark_recurring(payer_id: UUID, merchant: CurrentMerchant, conn: Conn):
    if not await stop_recurring(conn, merchant.id, payer_id):
        raise HTTPException(404, "No active recurring schedule for this customer")
    return {"stopped": True}
