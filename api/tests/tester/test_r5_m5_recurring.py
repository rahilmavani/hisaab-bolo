"""Tester round 5, M5: editing a schedule, failures while a due is created, clock jumps forward and
back, tracking-only for a customer with an email on file.

Spec: ARCHITECTURE.md moment 4 ("the merchant marks a payer recurring, and a missed due triggers a
reminder email"), Decisions/Recurring ("Track, auto-create dues and auto-remind. An optional email
popup appears when marking recurring. With no email, the schedule only tracks"), Collections flow
("Recurring schedules with an email create invoices per due date; without an email they only
track"), Risks ("The recurring demo needs time to pass: the clock jump"), API surface
(POST /payers/{id}/recurring, /jobs/tick, /admin/clock). UI copy that states the intended
behaviour: recurring-card.tsx ("Invoices and overdue reminders go to {email}."), recurring-dialog.tsx
("Monthly: Same date each month"; the edit dialog posts anchor_date = the schedule's next_due_date).

All writes use the throwaway `other_merchant`; ticks sweep every merchant, so the demo merchant is
re-seeded after the module and the clock is restored after each test that moves it.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, time, timedelta

import pytest

from app.money import IST
from app.services import email as email_svc
from app.services.email import EmailError
from app.services.paytm import PaytmError

from ..conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def _reseed_after_module(reseed_demo):
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def _clean_outbox():
    """Invoice numbers restart at INV-0001 for every throwaway merchant: start each test with an empty outbox."""
    email_svc.SENT.clear()
    yield


def today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


def cron(settings) -> dict:
    return {"X-Cron-Secret": settings.cron_secret}


async def mark(api, m, payer_key="payer_a", **body):
    payload = {"cadence": "weekly", "amount_paise": 1000_00, "grace_days": 0} | body
    r = await api.post(f"/payers/{m[payer_key]}/recurring", json=payload, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    return r.json()


async def jump(api, token, **body) -> dict:
    r = await api.post("/admin/clock", json=body, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def invoices_of(db, payer_id) -> list[dict]:
    return db.execute("select * from invoices where payer_id = %s order by due_date, created_at", (payer_id,)).fetchall()


def reminders_of(db, invoice_id, kind=None) -> list[dict]:
    sql = "select * from reminders where invoice_id = %s" + (" and kind = %s" if kind else "")
    return db.execute(sql, (invoice_id, kind) if kind else (invoice_id,)).fetchall()


def mails(number: str, prefix: str) -> list[dict]:
    return [x for x in email_svc.SENT if x["subject"].startswith(prefix) and number in x["html"]]


def schedule_of(db, payer_id) -> dict:
    return db.execute("select * from recurring_schedules where payer_id = %s and active", (payer_id,)).fetchone()


# ---------------------------------------------------------------------------
# Editing a schedule
# ---------------------------------------------------------------------------
def _next_month_end_case(start: date) -> tuple[date, date]:
    """First 31st on/after `start` whose next month is shorter and the month after has 31 days
    (e.g. 31 Oct → 30 Nov → 31 Dec). Returns (anchor, the 31st two months later)."""
    d = start
    while True:
        y, mth = d.year, d.month
        if calendar.monthrange(y, mth)[1] == 31 and date(y, mth, 31) >= start:
            ny, nm = (y + (mth + 1 > 12), (mth % 12) + 1)
            ay, am = (ny + (nm + 1 > 12), (nm % 12) + 1)
            if calendar.monthrange(ny, nm)[1] < 31 and calendar.monthrange(ay, am)[1] == 31:
                return date(y, mth, 31), date(ay, am, 31)
        d = date(y + (mth == 12), (mth % 12) + 1, 1)


async def test_editing_a_monthly_schedule_keeps_its_day_of_month(api, other_merchant, db, restore_clock):
    """recurring-dialog.tsx: Monthly is "Same date each month". A schedule anchored on the 31st bills
    31 Oct, 30 Nov, 31 Dec. Editing only the amount (the edit dialog posts anchor_date = next due,
    i.e. 30 Nov) must not move every later due to the 30th."""
    m = other_merchant
    anchor, later = _next_month_end_case(today(db))
    await jump(api, m["token"], set_to=datetime.combine(anchor, time(12, 30), tzinfo=IST).isoformat(), run_jobs=False)
    first = await mark(api, m, cadence="monthly", amount_paise=3000_00, anchor_date=anchor.isoformat(),
                       email="asha@example.com", grace_days=3)
    nxt = first["schedule"]["next_due_date"]
    assert first["dues_created"] and nxt != anchor.isoformat()
    # The edit dialog: First/Next due defaults to the schedule's next_due_date; only the amount changes.
    await mark(api, m, cadence="monthly", amount_paise=3200_00, anchor_date=nxt, email="asha@example.com", grace_days=3)
    await jump(api, m["token"], set_to=datetime.combine(later - timedelta(days=3), time(12, 30), tzinfo=IST).isoformat())
    dues = [i["due_date"] for i in invoices_of(db, m["payer_a"])]
    assert later in dues, (
        f"anchored on {anchor}, the schedule should bill {later} after an amount-only edit; dues are {dues} "
        f"(the edit re-anchored it on {nxt}, so later dues fall on day {date.fromisoformat(nxt).day})"
    )


async def test_overdue_reminders_follow_the_schedule_email_after_an_edit(api, other_merchant, db, restore_clock):
    """recurring-card.tsx promises "Invoices and overdue reminders go to {schedule email}." After
    the merchant edits the schedule's email (e.g. the first one was a typo), the reminder for the
    open due must go to the new address, not the old one."""
    m = other_merchant
    old, new = f"old-{uuid.uuid4().hex[:6]}@example.com", f"new-{uuid.uuid4().hex[:6]}@example.com"
    first = await mark(api, m, email=old)
    [due] = first["dues_created"]
    edited = await mark(api, m, email=new, anchor_date=first["schedule"]["next_due_date"])
    assert edited["schedule"]["email"] == new and edited["schedule"]["updated"]
    out = await jump(api, m["token"], days=1)
    assert due["invoice"] in out["jobs"]["overdue"]["reminded"]
    to = [x["intended_to"] for x in mails(due["invoice"], "Reminder:")]
    assert to == [new], f"the card says reminders go to {new}, but the reminder for {due['invoice']} went to {to}"


async def test_switching_an_edited_schedule_to_tracking_only_stops_reminders(api, other_merchant, db, restore_clock):
    m = other_merchant
    first = await mark(api, m, email="asha@example.com")
    [due] = first["dues_created"]
    await mark(api, m, email=None, anchor_date=first["schedule"]["next_due_date"])
    out = await jump(api, m["token"], days=1)
    assert due["invoice"] in out["jobs"]["overdue"]["overdue"]
    assert due["invoice"] not in out["jobs"]["overdue"]["reminded"] and not mails(due["invoice"], "Reminder:")


# ---------------------------------------------------------------------------
# Failures while a due is created
# ---------------------------------------------------------------------------
async def test_a_due_whose_invoice_email_failed_is_emailed_later(api, other_merchant, db, settings, restore_clock,
                                                                monkeypatch):
    """Moment 4 / Collections flow: a schedule with an email "creates invoices per due date" and
    emails them. If Resend fails while the due is created, the due exists but its invoice email
    never went out; the following ticks must still send it (overdue reminders already retry), and
    the mark response must not claim the due was "already invoiced"."""
    import app.services.invoices as inv_svc

    real_send = inv_svc.send_email
    state = {"failed": False}

    async def flaky(to, subject, body, attachments=None):
        if subject.startswith("Invoice ") and not state["failed"]:
            state["failed"] = True
            raise EmailError("Resend rejected the email (503): try again")
        return await real_send(to, subject, body, attachments)

    monkeypatch.setattr(inv_svc, "send_email", flaky)
    m = other_merchant
    r = await mark(api, m, email="asha@example.com", anchor_date=(today(db) + timedelta(days=2)).isoformat(),
                   grace_days=3)
    assert state["failed"]
    [inv] = invoices_of(db, m["payer_a"])
    for _ in range(3):  # three cron firings with Resend healthy again
        assert (await api.post("/jobs/tick", headers=cron(settings))).status_code == 200
    sent = reminders_of(db, inv["id"], "invoice")
    assert sent, (
        f"{inv['number']} (due {inv['due_date']}) was created while Resend was down and its invoice email was "
        f"never retried; the mark response said dues_created={r['dues_created']}, "
        f"already_invoiced={r['already_invoiced']} (the dialog shows '... is already invoiced.')"
    )


async def test_a_due_whose_payment_link_failed_gets_a_link_and_goes_overdue_later(api, other_merchant, db, settings,
                                                                                  restore_clock, monkeypatch):
    """Decisions/Recurring: "Track, auto-create dues and auto-remind." If Paytm fails once while a
    due is created, the next ticks must still give that due its link and QR, and a missed due must
    still be flagged and reminded."""
    from app.services.paytm.mock import MockPaytm

    real_create = MockPaytm.create_link
    state = {"failed": False}

    async def flaky(self, req):
        if not state["failed"]:
            state["failed"] = True
            raise PaytmError("503", "Service unavailable")
        return await real_create(self, req)

    monkeypatch.setattr(MockPaytm, "create_link", flaky)
    m = other_merchant
    r = await mark(api, m, email="asha@example.com")
    assert state["failed"]
    for _ in range(2):
        assert (await api.post("/jobs/tick", headers=cron(settings))).status_code == 200
    out = await jump(api, m["token"], days=2)
    [inv] = [i for i in invoices_of(db, m["payer_a"]) if i["due_date"] == today(db) - timedelta(days=2)]
    assert inv["status"] == "overdue" and inv["link_url"] and reminders_of(db, inv["id"], "overdue"), (
        f"after a one-off Paytm failure, {inv['number']} stays status={inv['status']!r} link_url={inv['link_url']!r}: "
        f"never sent, never overdue, never reminded (jobs: {out['jobs']['overdue']}); the mark response said "
        f"already_invoiced={r['already_invoiced']}"
    )


# ---------------------------------------------------------------------------
# Clock jumps forward and back
# ---------------------------------------------------------------------------
async def test_jumping_forward_resetting_and_jumping_again_bills_and_reminds_once(api, other_merchant, db,
                                                                                restore_clock):
    m = other_merchant
    await mark(api, m, email="asha@example.com")
    await jump(api, m["token"], days=8)
    first = {(i["due_date"], i["number"]) for i in invoices_of(db, m["payer_a"])}
    await jump(api, m["token"], reset=True)
    await jump(api, m["token"], days=8)
    invs = invoices_of(db, m["payer_a"])
    assert {(i["due_date"], i["number"]) for i in invs} == first, "the second +8 days created duplicate dues"
    assert len({i["due_date"] for i in invs}) == len(invs)
    for i in invs:
        if i["status"] == "overdue":
            assert len(reminders_of(db, i["id"], "overdue")) == 1, i["number"]
        assert len(reminders_of(db, i["id"], "invoice")) <= 1, i["number"]


async def test_tracking_only_never_emails_a_customer_who_has_an_email_on_file(api, other_merchant, db, restore_clock):
    """Decisions/Recurring: "With no email, the schedule only tracks (dues plus a missed flag)" -
    also when the customer has an email on their profile."""
    m = other_merchant
    r = await mark(api, m, email=None)
    assert r["schedule"]["tracking_only"]
    await jump(api, m["token"], days=8)
    invs = invoices_of(db, m["payer_a"])
    assert len(invs) == 2 and any(i["status"] == "overdue" for i in invs)
    for i in invs:
        assert not reminders_of(db, i["id"]) and not [x for x in email_svc.SENT if i["number"] in x["html"]], i["number"]
