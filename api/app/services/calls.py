"""Reminder calls: a Sarvam voice agent rings the customer about an unpaid invoice.

The agent runs on Sarvam's platform over the merchant's Twilio connection. We start a call with
Sarvam's instant-outbound API, passing the invoice as agent variables; Sarvam POSTs the outcome
(status, transcript, the agent's extracted answers) to /webhooks/sarvam/call when it ends.

A call is a reminder on another channel: it gets a `reminders` row (channel = 'call') straight
away as 'queued', and the webhook fills in how it went, with the agent's summary. A call can't
hand over a link, so when the customer wants to pay the agent uses its `send_payment_link` tool
(POST /webhooks/sarvam/tool/send-link) and the usual email with the Paytm link and QR goes out
mid-call; otherwise a connected call is followed by that email once it ends.

CALL_MODE=sarvam places real calls; mock pretends the customer picked up and promised to pay
(demo without keys); log only records the call (tests).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ..config import get_settings
from ..db import app_now, app_today, connection, fetch_all, fetch_one
from ..money import IST, format_inr
from ..phones import india_digits
from . import invoices as inv_svc
from .email import salutation

log = logging.getLogger(__name__)

SARVAM_BASE = "https://apps.sarvam.ai/api/outbounds/v1"
STATUSES = ("connected", "no_answer", "busy", "failed")
OUTCOMES = ("will_pay", "already_paid", "dispute", "call_later", "no_commitment")
IN_FLIGHT = timedelta(minutes=10)  # a queued call younger than this blocks another one
TOOL_WINDOW = timedelta(minutes=30)  # the agent's send-link tool works this long after the call starts
MAX_AUTO_ATTEMPTS = 2  # one call, plus one retry when nobody picked up
RETRY_AFTER = timedelta(hours=3)
PLACED: list[dict] = []  # log/mock-mode outbox, inspected by tests
_tasks: set[asyncio.Task] = set()


class CallError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class CallResult:
    attempt_id: str
    delivered_to: str
    intended_to: str


def e164(phone: str | None) -> str | None:
    """Stored phones are digits without +91 (see payers.normalise_phone): 10 digits is Indian."""
    if not phone or not phone.strip():
        return None
    digits = india_digits(phone)
    if len(digits) == 10:
        return f"+91{digits}"
    if 11 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def spoken_amount(paise: int) -> str:
    """'8,500 rupees' / '1,234 rupees 50 paise': what the agent reads out."""
    rupees, rest = divmod(paise, 100)
    words = f"{format_inr(rupees * 100)[1:]} rupees"
    return f"{words} {rest} paise" if rest else words


def _missing_config() -> list[str]:
    s = get_settings()
    required = {
        "SARVAM_AGENTS_API_KEY": s.sarvam_agents_api_key, "SARVAM_ORG_ID": s.sarvam_org_id,
        "SARVAM_WORKSPACE_ID": s.sarvam_workspace_id, "SARVAM_APP_ID": s.sarvam_app_id,
        "SARVAM_CONNECTION_ID": s.sarvam_connection_id, "SARVAM_AGENT_PHONE": s.sarvam_agent_phone,
    }
    return [name for name, value in required.items() if not value]


def webhook_url() -> str:
    s = get_settings()
    return f"{s.public_api_base}/webhooks/sarvam/call?token={s.call_webhook_secret}"


async def place_call(intended_to: str, variables: dict[str, str], metadata: dict[str, str]) -> CallResult:
    s = get_settings()
    to = e164(s.demo_customer_phone) or intended_to
    if s.call_mode != "sarvam":
        attempt_id = f"{s.call_mode}-{uuid.uuid4().hex[:16]}"
        PLACED.append({"attempt_id": attempt_id, "to": to, "intended_to": intended_to,
                       "variables": variables, "metadata": metadata})
        log.info("call (%s mode) to=%s intended=%s vars=%s", s.call_mode, to, intended_to, variables)
        return CallResult(attempt_id=attempt_id, delivered_to=to, intended_to=intended_to)
    if missing := _missing_config():
        raise CallError(503, f"Calling isn't set up yet: add {', '.join(missing)} to the API's environment")
    app_config: dict = {
        "app_id": s.sarvam_app_id,
        "app_version": s.sarvam_app_version,
        "connection_config": {"connection_id": s.sarvam_connection_id, "agent_phone_number": s.sarvam_agent_phone},
        "agent_variables": variables,
    }
    if s.sarvam_call_language:
        app_config["app_overrides"] = {"initial_language_name": s.sarvam_call_language}
    payload = {
        "app_config": app_config,
        "user_config": {"user_phone_number": to},
        "webhook_config": {"url": webhook_url(), "metadata": metadata},
    }
    url = f"{SARVAM_BASE}/orgs/{s.sarvam_org_id}/workspaces/{s.sarvam_workspace_id}/outbounds"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=payload, headers={"X-API-Key": s.sarvam_agents_api_key})
    except httpx.HTTPError as e:
        raise CallError(502, f"Couldn't reach Sarvam to place the call: {e}") from e
    if r.status_code >= 300:
        log.error("sarvam outbound %s: %s", r.status_code, r.text[:500])
        raise CallError(502, f"Sarvam refused the call ({r.status_code}): {r.text[:200]}")
    attempt_id = str(r.json().get("attempt_id") or "")
    if not attempt_id:
        raise CallError(502, f"Sarvam didn't return a call id: {r.text[:200]}")
    log.info("sarvam call %s to %s for %s", attempt_id, to, metadata.get("invoice_number"))
    return CallResult(attempt_id=attempt_id, delivered_to=to, intended_to=intended_to)


def mask_email(email: str) -> str:
    """'accounts@sharmatraders.in' -> 'a*******@sharmatraders.in': enough for the customer to
    recognise, without the agent reading out a full address."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}{'*' * max(len(local) - 1, 1)}@{domain}" if domain else email


def call_variables(inv: dict, merchant_name: str, today: date, kind: str, call_token: str) -> dict[str, str]:
    """What the agent knows about this call. Declare the same names on the agent's Variables tab."""
    due = inv["due_date"]
    email = inv["email_to"] or inv["payer_email"]
    return {
        "customer_name": salutation(inv["payer_name"]),
        "merchant_name": merchant_name,
        "invoice_number": inv["number"],
        "amount": spoken_amount(inv["amount_paise"]),
        "due_date": f"{due.day} {due.strftime('%B')}",
        "days_overdue": str(max((today - due).days, 0)),
        "reminder_kind": "overdue" if kind == "overdue" else "upcoming",
        "email_on_file": "yes" if email else "no",
        "email_hint": mask_email(email) if email else "",
        # Pass this, unchanged, as the body of the send_payment_link tool.
        "call_token": call_token,
    }


async def call_invoice(conn: AsyncConnection, merchant_id: UUID, invoice_id: UUID) -> dict:
    """Ring the customer about one unpaid invoice now."""
    inv = await inv_svc.get_invoice(conn, merchant_id, invoice_id)
    if inv is None:
        raise CallError(404, "Invoice not found")
    if inv["status"] not in ("sent", "overdue"):
        raise CallError(409, f"Invoice {inv['number']} is {inv['status']}; only unpaid invoices get calls")
    to = e164(inv["payer_phone"])
    if not to:
        raise CallError(422, f"{inv['payer_name']} has no phone number. Add one to call them.")
    busy = await fetch_one(
        conn,
        "select 1 from reminders where invoice_id = %s and channel = 'call' and call_status = 'queued' "
        "and sent_at > app_now() - %s",
        (invoice_id, IN_FLIGHT),
    )
    if busy:
        raise CallError(409, f"A call to {inv['payer_name']} about {inv['number']} is already in progress")
    merchant = await fetch_one(conn, "select name from merchants where id = %s", (merchant_id,))
    today = await app_today(conn)
    kind = "overdue" if inv["status"] == "overdue" or inv["due_in_days"] < 0 else "invoice"
    token = "ct_" + secrets.token_urlsafe(18)
    # The row exists before the phone rings, so the agent's send-link tool can find it mid-call.
    reminder = await fetch_one(
        conn,
        "insert into reminders (merchant_id, invoice_id, kind, channel, to_phone, sent_at, call_status, call_token) "
        "values (%s, %s, %s, 'call', %s, %s, 'queued', %s) returning id",
        (merchant_id, invoice_id, kind, to, await app_now(conn), token),
    )
    try:
        placed = await place_call(
            to, call_variables(inv, merchant["name"], today, kind, token),
            {"invoice_id": str(invoice_id), "invoice_number": inv["number"]},
        )
    except Exception:
        await conn.execute("delete from reminders where id = %s", (reminder["id"],))
        raise
    reminder = await fetch_one(
        conn, "update reminders set call_attempt_id = %s, to_phone = %s where id = %s returning *",
        (placed.attempt_id, placed.delivered_to, reminder["id"]),
    )
    if get_settings().call_mode == "mock":
        _later(_mock_outcome(placed.attempt_id))
    return {"reminder": reminder, "delivered_to": placed.delivered_to, "intended_to": placed.intended_to}


def _parse_date(value) -> date | None:
    text = str(value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


async def record_call_outcome(conn: AsyncConnection, payload: dict) -> dict:
    """Apply Sarvam's end-of-call webhook. Safe to call any number of times."""
    attempt_id = str(payload.get("attempt_id") or "")
    row = await fetch_one(
        conn,
        "select r.id, r.call_status, r.invoice_id, r.merchant_id, r.call_link_sent_at from reminders r "
        "where r.call_attempt_id = %s",
        (attempt_id,),
    ) if attempt_id else None
    if row is None:
        log.warning("sarvam webhook for unknown attempt %r", attempt_id)
        return {"result": "unmatched"}
    if row["call_status"] != "queued":
        return {"result": "duplicate", "status": row["call_status"]}
    status = payload.get("status") if payload.get("status") in STATUSES else "failed"
    answers = payload.get("final_agent_variables") or {}
    outcome = str(answers.get("outcome") or "").strip().lower() or None
    if outcome and outcome not in OUTCOMES:
        outcome = "no_commitment"
    transcript = payload.get("interaction_transcript")
    duration = payload.get("duration")
    await conn.execute(
        """
        update reminders set call_status = %s, call_duration_s = %s, call_outcome = %s, call_promised_date = %s,
               call_transcript = %s, call_failure = %s, call_summary = %s, call_ended_at = app_now()
         where id = %s
        """,
        (status, int(duration) if isinstance(duration, int | float) else None, outcome,
         _parse_date(answers.get("promised_date")), Jsonb(transcript) if transcript else None,
         str(payload["failure_reason"]) if payload.get("failure_reason") else None,
         (str(answers.get("summary") or "").strip()[:1000] or None), row["id"]),
    )
    out = {"result": "recorded", "status": status, "outcome": outcome}
    if row["call_link_sent_at"]:
        out["link_emailed"] = "during_call"
    elif status == "connected":
        out["link_emailed"] = await _email_link_after_call(conn, row["merchant_id"], row["invoice_id"], row["id"])
    return out


async def _email_link_after_call(conn: AsyncConnection, merchant_id: UUID, invoice_id: UUID, call_id: UUID) -> bool:
    """A customer who picked up gets the link by email even if they didn't ask for it mid-call."""
    inv = await inv_svc.get_invoice(conn, merchant_id, invoice_id)
    if not inv or inv["status"] not in ("sent", "overdue") or not (inv["email_to"] or inv["payer_email"]):
        return False
    try:
        await inv_svc.remind(conn, merchant_id, invoice_id, during_call=call_id)
    except inv_svc.InvoiceError as e:
        log.info("link email after call for %s failed: %s", inv["number"], e)
        return False
    return True


async def send_link_during_call(conn: AsyncConnection, call_token: str) -> dict:
    """The agent's `send_payment_link` tool: the customer wants to pay now, so email them the Paytm
    link and QR for this call's invoice. Only this call's invoice, only to the customer's own email
    on file, at most once per call. The reply is what the agent tells the customer."""
    row = await fetch_one(
        conn,
        "select id, merchant_id, invoice_id, call_link_sent_at, sent_at from reminders "
        "where call_token = %s and channel = 'call'",
        (call_token,),
    ) if call_token else None
    if row is None or row["sent_at"] < await app_now(conn) - TOOL_WINDOW:
        return {"sent": False, "reason": "unknown_call",
                "message": "I can't send the link from here right now; the shop will share it with you."}
    inv = await inv_svc.get_invoice(conn, row["merchant_id"], row["invoice_id"])
    to = (inv["email_to"] or inv["payer_email"]) if inv else None
    if row["call_link_sent_at"]:
        return {"sent": True, "reason": "already_sent", "email": mask_email(to or ""),
                "message": f"The payment link is already in your email ({mask_email(to or '')})."}
    if inv is None or inv["status"] == "paid":
        return {"sent": False, "reason": "already_paid", "message": "This invoice is already paid. Thank you!"}
    if inv["status"] not in ("sent", "overdue"):
        return {"sent": False, "reason": "not_open", "message": "This invoice is no longer due; no payment is needed."}
    if not to:
        return {"sent": False, "reason": "no_email",
                "message": "There's no email address on file, so the shop will share the payment link with you."}
    try:
        await inv_svc.remind(conn, row["merchant_id"], row["invoice_id"], during_call=row["id"])
    except inv_svc.InvoiceError as e:
        log.warning("send-link tool for %s failed: %s", inv["number"], e)
        return {"sent": False, "reason": "send_failed",
                "message": "The email didn't go through just now; the shop will share the link with you."}
    await conn.execute("update reminders set call_link_sent_at = app_now() where id = %s", (row["id"],))
    log.info("agent emailed the link for %s during the call", inv["number"])
    return {"sent": True, "reason": "sent", "email": mask_email(to),
            "message": f"Done: the Paytm payment link and QR for {spoken_amount(inv['amount_paise'])} "
                       f"are in your email ({mask_email(to)}). You can pay with any UPI app."}


async def promised_payments(conn: AsyncConnection, merchant_id: UUID) -> list[dict]:
    """Unpaid invoices whose latest answered call ended in a promise to pay, soonest first."""
    rows = await fetch_all(
        conn,
        """
        select * from (
          select distinct on (i.id)
                 i.id as invoice_id, i.number, i.amount_paise, i.status, i.payer_id, p.display_name as payer_name,
                 r.call_promised_date as promised_date, r.call_outcome as outcome, r.sent_at as called_at,
                 r.call_link_sent_at, r.call_summary
            from reminders r
            join invoices i on i.id = r.invoice_id
            join payers p on p.id = i.payer_id
           where r.merchant_id = %s and r.channel = 'call' and r.call_status = 'connected'
             and i.status in ('sent', 'overdue')
           order by i.id, r.sent_at desc
        ) latest
         where outcome = 'will_pay' or promised_date is not null
         order by promised_date nulls last, called_at
        """,
        (merchant_id,),
    )
    today = await app_today(conn)
    for r in rows:
        d = r["promised_date"]
        r["promise_state"] = "none" if d is None else "broken" if d < today else "today" if d == today else "upcoming"
    return rows


def within_call_window(now: datetime) -> bool:
    start, end = get_settings().call_window_ist
    return start <= now.astimezone(IST).hour < end


async def call_overdue(conn: AsyncConnection) -> dict:
    """Tick step: call customers whose invoice is still unpaid a while after the overdue email.
    Only invoices that opted in, only in daytime, and at most two attempts (a retry if nobody
    picked up). A call Sarvam never reported on is not repeated."""
    candidates = await fetch_all(
        conn,
        """
        select i.id, i.merchant_id, i.number from invoices i join payers p on p.id = i.payer_id
         where i.status = 'overdue' and i.call_remind and coalesce(p.phone, '') <> ''
           and (i.recurring_id is null or exists (select 1 from recurring_schedules s
                                                   where s.id = i.recurring_id and s.active and s.call_remind))
           and i.due_date + i.grace_days + 1 + %(after)s <= app_today()
           and (select count(*) from reminders r where r.invoice_id = i.id and r.channel = 'call') < %(max)s
           and not exists (select 1 from reminders r where r.invoice_id = i.id and r.channel = 'call'
                             and (r.call_status not in ('no_answer', 'busy') or r.sent_at > app_now() - %(retry)s))
         order by i.due_date
         limit 20
        """,
        {"after": get_settings().call_after_overdue_days, "max": MAX_AUTO_ATTEMPTS, "retry": RETRY_AFTER},
    )
    if not candidates:
        return {"called": [], "retry_later": []}
    if not within_call_window(await app_now(conn)):
        return {"called": [], "retry_later": [], "waiting_for_daytime": [c["number"] for c in candidates]}
    called, failed = [], []
    for c in candidates:
        try:
            await call_invoice(conn, c["merchant_id"], c["id"])
            called.append(c["number"])
        except CallError as e:
            log.info("reminder call for %s will be retried: %s", c["number"], e)
            failed.append(c["number"])
    return {"called": called, "retry_later": failed}


# ---------------------------------------------------------------------------
# Mock mode: the customer picks up and promises to pay in two days.
# ---------------------------------------------------------------------------
def _later(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _mock_outcome(attempt_id: str, delay_s: float = 4) -> None:
    await asyncio.sleep(delay_s)
    try:
        async with connection() as conn:
            r = await fetch_one(
                conn,
                "select i.number, i.amount_paise, p.display_name, m.name as merchant, r.call_token from reminders r "
                "join invoices i on i.id = r.invoice_id join payers p on p.id = i.payer_id "
                "join merchants m on m.id = r.merchant_id where r.call_attempt_id = %s",
                (attempt_id,),
            )
            if r is None:
                return
            amount = spoken_amount(r["amount_paise"])
            opening = {"role": "agent", "en_text": f"Namaste {salutation(r['display_name'])}, I'm calling from "
                                                   f"{r['merchant']} about invoice {r['number']} for {amount}."}
            # Like the real agent: a customer with an email asks for the link and pays now.
            tool = await send_link_during_call(conn, r["call_token"]) if r["call_token"] else {"sent": False}
            today = await app_today(conn)
            if tool["sent"]:
                promised, summary = today, (f"{r['display_name']} asked for the payment link to pay right away; "
                                            f"the agent emailed it during the call.")
                lines = [opening, {"role": "user", "en_text": "Haan, link bhej do, main abhi kar deta hoon."},
                         {"role": "agent", "en_text": tool["message"]},
                         {"role": "user", "en_text": "Mil gaya, thank you."}]
            else:
                promised = today + timedelta(days=2)
                summary = (f"{r['display_name']} had forgotten and promised to pay by "
                           f"{promised.day} {promised.strftime('%B')}.")
                lines = [opening, {"role": "user", "en_text": "Haan, I forgot. I'll pay it day after tomorrow."},
                         {"role": "agent", "en_text": f"Thank you. I've noted {promised.day} {promised.strftime('%B')}."}]
            await record_call_outcome(conn, {
                "attempt_id": attempt_id,
                "status": "connected",
                "duration": 38 + secrets.randbelow(30),
                "final_agent_variables": {"outcome": "will_pay", "promised_date": promised.isoformat(),
                                          "summary": summary},
                "interaction_transcript": lines,
            })
    except Exception:
        log.exception("mock call outcome failed for %s", attempt_id)
