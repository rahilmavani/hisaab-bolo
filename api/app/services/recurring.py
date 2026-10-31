"""Recurring payers: track, auto-create dues, auto-remind.

Every due is an invoice (with its own order ID, Paytm link and QR) tied to the
schedule. With an email the invoice is emailed and a missed due gets a reminder;
without one the schedule only tracks (the due and its overdue "missed" flag).
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from ..db import app_today, fetch_all, fetch_one
from ..money import format_inr
from . import invoices as inv_svc
from .payers import add_identifier, conflict_message, identifier_owner

log = logging.getLogger(__name__)

LEAD_DAYS = 3  # create (and email) the invoice this many days before the due date



def _reminded_while_overdue(grace_sql: str = "i.grace_days") -> str:
    """SQL: the due already got an overdue reminder once it was actually overdue (due date + grace
    passed). A manual nudge during the grace period doesn't stand in for the automatic reminder."""
    return ("exists (select 1 from reminders r where r.invoice_id = i.id and r.kind = 'overdue' "
            f"and r.sent_at >= (i.due_date + {grace_sql} + 1)::timestamp at time zone 'Asia/Kolkata')")
MAX_CATCH_UP = 12  # dues created per schedule per tick after a long clock jump


class RecurringError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def next_due(current: date, cadence: str, anchor: date) -> date:
    if cadence == "weekly":
        return current + timedelta(days=7)
    year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


async def set_recurring(
    conn: AsyncConnection,
    merchant_id: UUID,
    payer_id: UUID,
    *,
    cadence: str,
    amount_paise: int,
    anchor_date: date | None = None,
    grace_days: int = 3,
    email: str | None = None,
    auto_remind: bool = True,
) -> dict:
    """Mark a customer recurring, or edit their active schedule in place.

    The schedule's own email (typed in the popup) decides what happens: with one, each due is
    invoiced and emailed and a missed due is reminded; without one, the schedule only tracks
    dues and flags missed ones, even if the customer has an email on file."""
    payer = await fetch_one(
        conn, "select * from payers where id = %s and merchant_id = %s and merged_into is null", (payer_id, merchant_id)
    )
    if payer is None:
        raise RecurringError(404, "Customer not found")
    if amount_paise < 100:
        raise RecurringError(422, "The amount must be at least ₹1")
    today = await app_today(conn)
    email = (email or "").strip().lower() or None
    if email and (owner := await identifier_owner(conn, merchant_id, "email", email, payer_id)):
        raise RecurringError(409, conflict_message(email, owner))
    # One transaction: a refused request (e.g. a past first due) leaves the customer untouched.
    async with conn.transaction():
        existing = await fetch_one(
            conn, "select * from recurring_schedules where payer_id = %s and active for update", (payer_id,)
        )
        if existing and anchor_date in (None, existing["next_due_date"]):
            # Same next due: keep the original anchor, so a monthly due on the 31st stays on the 31st
            # (30 Nov is only a clamp). A cadence change re-anchors on the next due.
            anchor, next_date = (existing["anchor_date"] if cadence == existing["cadence"]
                                 else existing["next_due_date"]), existing["next_due_date"]
        else:
            anchor = next_date = anchor_date or today
            if anchor < today:
                raise RecurringError(422, "The first due date can't be in the past")
        if email:
            await conn.execute("update payers set email = coalesce(email, %s) where id = %s", (email, payer_id))
            await add_identifier(conn, merchant_id, payer_id, "email", email)
        remind = auto_remind and bool(email)
        if existing:
            schedule = await fetch_one(
                conn,
                """
                update recurring_schedules
                   set amount_paise = %s, cadence = %s, anchor_date = %s, next_due_date = %s, grace_days = %s,
                       email = %s, auto_remind = %s
                 where id = %s returning *
                """,
                (amount_paise, cadence, anchor, next_date, grace_days, email, remind, existing["id"]),
            )
        else:
            schedule = await fetch_one(
                conn,
                """
                insert into recurring_schedules (merchant_id, payer_id, amount_paise, cadence, anchor_date,
                                                 next_due_date, grace_days, email, auto_remind, active)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true) returning *
                """,
                (merchant_id, payer_id, amount_paise, cadence, anchor, anchor, grace_days, email,
                 auto_remind and bool(email)),
            )
        assert schedule
        await _adopt_open_dues(conn, payer_id, schedule)
    schedule["tracking_only"] = not email
    schedule["updated"] = bool(existing)
    return schedule


async def _adopt_open_dues(conn: AsyncConnection, payer_id: UUID, schedule: dict) -> None:
    """The active schedule covers all of the customer's unpaid dues, including ones billed before it
    was edited or before an earlier schedule was stopped and marked again: they take its email,
    grace and reminder setting. A missed due that was never reminded gets its reminder (on the next
    tick); one that more grace puts back inside its grace period is open again."""
    await conn.execute(
        f"""
        update invoices i
           set recurring_id = %(sid)s,
               email_to = %(email)s,
               grace_days = %(grace)s,
               status = case when i.status = 'overdue' and i.due_date + %(grace)s >= app_today()
                             then 'sent' else i.status end,
               overdue_reminder_pending = i.status = 'overdue' and i.due_date + %(grace)s < app_today() and %(remind)s
                 and not {_reminded_while_overdue("%(grace)s")}
         where i.payer_id = %(pid)s and i.recurring_id is not null and i.status in ('sent', 'overdue')
        """,
        {"sid": schedule["id"], "email": schedule["email"], "grace": schedule["grace_days"],
         "remind": schedule["auto_remind"], "pid": payer_id},
    )


async def stop_recurring(conn: AsyncConnection, merchant_id: UUID, payer_id: UUID) -> bool:
    cur = await conn.execute(
        "update recurring_schedules set active = false where payer_id = %s and merchant_id = %s and active",
        (payer_id, merchant_id),
    )
    return cur.rowcount > 0


async def create_due_invoices(
    conn: AsyncConnection, schedule_id: UUID | None = None, errors: list[str] | None = None
) -> list[dict]:
    """Turn upcoming dues into invoices. Catches up after the demo clock jumps.

    One invoice per customer and due date, even across a stopped and re-created schedule.
    Dues already past their grace period when created are not emailed as invoices: the overdue step sends
    one reminder for them instead of two emails at once. A due whose Paytm link fails is not
    created (nothing is left behind) and is tried again on the next tick; failures are appended
    to `errors` when given."""
    today = await app_today(conn)
    schedules = await fetch_all(
        conn,
        """
        select s.*, p.display_name as payer_name
          from recurring_schedules s join payers p on p.id = s.payer_id
         where s.active and s.next_due_date <= %s and (%s::uuid is null or s.id = %s)
        """,
        (today + timedelta(days=LEAD_DAYS), schedule_id, schedule_id),
    )
    created = []
    for s in schedules:
        due = s["next_due_date"]
        n = 0
        while due <= today + timedelta(days=LEAD_DAYS) and n < MAX_CATCH_UP:
            exists = await fetch_one(
                conn,
                "select id from invoices where payer_id = %s and recurring_id is not null and due_date = %s "
                "and status <> 'cancelled'",
                (s["payer_id"], due),
            )
            if exists is None:
                label = "Weekly" if s["cadence"] == "weekly" else "Monthly"
                # Past its due date but inside the grace period: still worth the invoice email. Beyond
                # grace, the overdue reminder is the one email (not two at once).
                emailed = bool(s["email"]) and due + timedelta(days=s["grace_days"]) >= today
                try:
                    inv = await inv_svc.create_invoice(
                        conn, s["merchant_id"],
                        payer_id=s["payer_id"], due_date=due,
                        line_items=[{"name": f"{label} payment, due {due.day} {due.strftime('%b')}", "qty": 1,
                                     "unit_price_paise": s["amount_paise"]}],
                        email=s["email"], send=emailed, recurring_id=s["id"], grace_days=s["grace_days"],
                        allow_past_due=True,
                    )
                except UniqueViolation as e:
                    if e.diag.constraint_name != "invoices_one_per_recurring_due":
                        raise
                    inv = None  # billed a moment ago by a concurrent tick or "Mark recurring"
                except Exception as e:  # Paytm, Storage, anything: retry next tick, other schedules go on
                    log.exception("recurring due for %s failed", s["payer_name"])
                    if errors is not None:
                        errors.append(str(e) if isinstance(e, inv_svc.InvoiceError)
                                      else "something went wrong on our side")
                    break
                if inv:
                    created.append({"invoice": inv["number"], "payer": s["payer_name"], "due_date": due,
                                    "amount": format_inr(s["amount_paise"]),
                                    "emailed": emailed and not inv["email_error"], "email_error": inv["email_error"]})
            due = next_due(due, s["cadence"], s["anchor_date"])
            n += 1
        await conn.execute("update recurring_schedules set next_due_date = %s where id = %s", (due, s["id"]))
    return created


async def mark_overdue_and_remind(conn: AsyncConnection) -> dict:
    """Flip dues past their grace period to overdue and queue one reminder for each that should
    get one; queued reminders are retried on every tick until an email actually goes out."""
    newly = await fetch_all(
        conn,
        f"""
        update invoices i
           set status = 'overdue',
               overdue_reminder_pending = case
                 when {_reminded_while_overdue()} then false
                 when i.recurring_id is null then coalesce(i.email_to, '') <> ''
                 else exists (select 1 from recurring_schedules s where s.id = i.recurring_id
                                and s.active and s.auto_remind and s.email is not null)
               end
         where i.status = 'sent' and i.due_date + i.grace_days < app_today()
        returning i.number
        """,
    )
    pending = await fetch_all(
        conn,
        """
        select i.id, i.merchant_id, i.number from invoices i
         where i.status = 'overdue' and i.overdue_reminder_pending
           and (i.recurring_id is null or exists (select 1 from recurring_schedules s where s.id = i.recurring_id
                                                    and s.active and s.auto_remind))
         order by i.due_date
        """,
    )
    reminded, failed = [], []
    for inv in pending:
        try:
            await inv_svc.send_invoice_email(conn, inv["merchant_id"], inv["id"], kind="overdue")
        except inv_svc.InvoiceError as e:
            log.info("overdue reminder for %s will be retried: %s", inv["number"], e)
            failed.append(inv["number"])
            continue
        await conn.execute("update invoices set overdue_reminder_pending = false where id = %s", (inv["id"],))
        reminded.append(inv["number"])
    newly_numbers = [i["number"] for i in newly]
    return {"overdue": newly_numbers, "reminded": reminded, "retry_later": failed,
            "not_reminded": [n for n in newly_numbers if n not in reminded and n not in failed]}


async def send_unsent_due_emails(conn: AsyncConnection, payer_id: UUID | None = None) -> dict:
    """Retry invoice emails for upcoming recurring dues that never went out (a failed send, or an
    email added to the schedule after the due was created)."""
    rows = await fetch_all(
        conn,
        """
        select i.id, i.merchant_id, i.number from invoices i
          join recurring_schedules s on s.id = i.recurring_id
         where i.status = 'sent' and i.sent_at is null and coalesce(i.email_to, '') <> ''
           and i.due_date + i.grace_days >= app_today() and s.active and s.email is not null
           and not exists (select 1 from reminders r where r.invoice_id = i.id)
           and (%(pid)s::uuid is null or i.payer_id = %(pid)s)
         order by i.due_date
        """,
        {"pid": payer_id},
    )
    sent, failed = [], []
    for inv in rows:
        try:
            await inv_svc.send_invoice_email(conn, inv["merchant_id"], inv["id"], kind="invoice")
            sent.append(inv["number"])
        except inv_svc.InvoiceError as e:
            log.info("invoice email for %s will be retried: %s", inv["number"], e)
            failed.append(inv["number"])
    return {"sent": sent, "retry_later": failed}


async def reopen_not_yet_overdue(conn: AsyncConnection) -> list[str]:
    """After the demo clock moves back, dues that are no longer past their grace period are open
    again. Reminders already sent stay in the history and are not sent twice."""
    rows = await fetch_all(
        conn,
        """
        update invoices set status = 'sent', overdue_reminder_pending = false
         where status = 'overdue' and due_date + grace_days >= app_today()
        returning number
        """,
    )
    return [r["number"] for r in rows]
