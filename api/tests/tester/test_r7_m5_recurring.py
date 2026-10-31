"""Tester round 7, M5: recurring dues billed late by a clock jump, a manual nudge inside the grace period,
a background tick racing "Mark recurring", the in-process scheduler, and a quick re-run of moment 4.

Spec: ARCHITECTURE.md moment 4 ("the merchant marks a payer recurring, and a missed due triggers a
reminder email"), Decisions/Recurring ("Track, auto-create dues and auto-remind"), Collections flow
("Overdue (due + grace) triggers a reminder email with the same link. Recurring schedules with an email
create invoices per due date"), API surface (POST /payers/{id}/recurring, POST /jobs/tick "cron every 5
minutes", POST /admin/clock), Risks ("The recurring demo needs time to pass: the clock jump").
UI copy that states the intended behaviour:
  recurring-dialog.tsx  "Each due gets an invoice with a Paytm link and QR, emailed to {email} 3 days
                         ahead. If it's still unpaid {grace} days after the due date, a reminder goes out
                         the following day."
  recurring-card.tsx    "Invoices and overdue reminders go to {email}."
  collections/[id]      "Not emailed yet: the invoice email to {email} hasn't gone out."

Writes use the throwaway `other_merchant`; clock jumps run the jobs for every merchant, so the demo
merchant is re-seeded after the module and the clock is restored after each test that moves it.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from app.db import connection
from app.services import email as email_svc
from app.services import jobs as jobs_svc
from app.services.recurring import create_due_invoices, set_recurring

from ..conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def _reseed_after_module(reseed_demo):
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def _clean_outbox():
    email_svc.SENT.clear()
    yield


def today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


async def mark(api, m, payer_key="payer_a", expect=201, **body):
    payload = {"cadence": "weekly", "amount_paise": 800_00, "grace_days": 3, "email": "asha@example.com"} | body
    r = await api.post(f"/payers/{m[payer_key]}/recurring", json=payload, headers=bearer(m["token"]))
    assert r.status_code == expect, r.text
    return r.json()


async def jump(api, token, **body) -> dict:
    r = await api.post("/admin/clock", json=body, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def invoices_of(db, payer_id) -> list[dict]:
    return db.execute("select * from invoices where payer_id = %s order by due_date, created_at", (payer_id,)).fetchall()


def reminders_of(db, invoice_id, kind=None) -> list[dict]:
    sql = "select * from reminders where invoice_id = %s" + (" and kind = %s" if kind else "") + " order by sent_at"
    return db.execute(sql, (invoice_id, kind) if kind else (invoice_id,)).fetchall()


def emails_about(number: str) -> list[dict]:
    return [e for e in email_svc.SENT if f"#{number}" in e["html"] or f"Invoice {number} " in e["subject"]]


# ---------------------------------------------------------------------------
# Moment 4, quickly re-verified end to end through the API
# ---------------------------------------------------------------------------
async def test_moment4_mark_with_email_jump_one_reminder_stop_and_reset(api, other_merchant, db, restore_clock):
    m = other_merchant
    base = db.execute("select base_override_now, override_now from app_clock where id = 1").fetchone()
    out = await mark(api, m, grace_days=2)
    [due] = out["dues_created"]
    assert due["emailed"] is True and out["schedule"]["auto_remind"] is True
    d0 = invoices_of(db, m["payer_a"])[0]
    assert d0["email_to"] == "asha@example.com" and d0["link_url"] and d0["qr_image_url"]

    await jump(api, m["token"], days=7)  # due + 2 days grace has passed
    d0 = db.execute("select * from invoices where id = %s", (d0["id"],)).fetchone()
    assert d0["status"] == "overdue"
    assert len(reminders_of(db, d0["id"], "overdue")) == 1
    reminder = [e for e in email_svc.SENT if e["subject"].startswith("Reminder:")]
    assert reminder and d0["link_url"] in reminder[0]["html"]  # same link

    await jump(api, m["token"], days=1)
    await jump(api, m["token"], days=1)
    assert len(reminders_of(db, d0["id"], "overdue")) == 1  # exactly one

    r = await api.delete(f"/payers/{m['payer_a']}/recurring", headers=bearer(m["token"]))
    assert r.status_code == 200
    await jump(api, m["token"], reset=True)
    after = db.execute("select base_override_now, override_now from app_clock where id = 1").fetchone()
    assert after["override_now"] == base["base_override_now"]


# ---------------------------------------------------------------------------
# A due billed after its date (clock jump / downtime) but still inside its grace period
# ---------------------------------------------------------------------------
async def test_a_due_billed_late_but_inside_its_grace_is_emailed_to_the_customer(api, other_merchant, db, settings,
                                                                                   restore_clock):
    """Weekly, 3 days grace, with an email. The demo clock goes +1 day, then +7 days: the second due
    (D+7) is created on D+8, one day past its date and still inside its 3-day grace period.
    The popup promises "Each due gets an invoice ... emailed to {email}", and the card says
    "Invoices and overdue reminders go to {email}". The due is neither overdue (so no reminder) nor
    emailed as an invoice; send_unsent_due_emails skips it too (due_date < today), so the customer
    hears nothing about it until the grace period runs out."""
    m = other_merchant
    d0 = today(db)
    await mark(api, m)
    await jump(api, m["token"], days=1)
    out = await jump(api, m["token"], days=7)
    created = [d for d in out["jobs"]["dues_created"] if isinstance(d, dict) and d["payer"] == "Asha Test"]
    assert [d["due_date"] for d in created] == [(d0 + timedelta(days=7)).isoformat()]
    late = db.execute("select * from invoices where payer_id = %s and due_date = %s",
                      (m["payer_a"], d0 + timedelta(days=7))).fetchone()
    assert late["status"] == "sent"  # inside grace: not overdue yet

    r = await api.post("/jobs/tick", headers={"X-Cron-Secret": settings.cron_secret})  # the next 5-minute run
    assert r.status_code == 200
    late = db.execute("select * from invoices where id = %s", (late["id"],)).fetchone()
    assert late["sent_at"] is not None and emails_about(late["number"]), (
        f"{late['number']} (due {late['due_date']}, {late['grace_days']} days grace) was created a day late and is "
        f"never emailed: sent_at={late['sent_at']}, reminders={len(reminders_of(db, late['id']))}, "
        f"emails mentioning it={len(emails_about(late['number']))}. The invoice page will say 'Not emailed yet'.")


# ---------------------------------------------------------------------------
# A manual nudge while the due is inside its grace period
# ---------------------------------------------------------------------------
async def test_a_manual_nudge_inside_grace_does_not_cancel_the_automatic_overdue_reminder(api, other_merchant, db,
                                                                                         restore_clock):
    """The popup says "If it's still unpaid 3 days after the due date, a reminder goes out the following
    day." The merchant presses "Send reminder" one day after the due date (still in grace). When the
    due is still unpaid after the grace period, the promised automatic reminder must still go out."""
    m = other_merchant
    await mark(api, m)
    [d0] = invoices_of(db, m["payer_a"])
    await jump(api, m["token"], days=1)
    r = await api.post(f"/invoices/{d0['id']}/remind", headers=bearer(m["token"]))
    assert r.status_code == 200, r.text
    nudges = reminders_of(db, d0["id"])
    assert [x["kind"] for x in nudges] == ["invoice", "overdue"]  # the manual nudge is filed as "overdue"

    out = await jump(api, m["token"], days=3)  # D+4: due + 3 days grace has passed
    d0 = db.execute("select * from invoices where id = %s", (d0["id"],)).fetchone()
    assert d0["status"] == "overdue" and d0["number"] in out["jobs"]["overdue"]["overdue"]
    after_grace = [x for x in reminders_of(db, d0["id"]) if x["sent_at"] > nudges[-1]["sent_at"]]
    assert after_grace, (
        f"{d0['number']} went overdue after its 3-day grace but no reminder went out "
        f"(jobs said: {out['jobs']['overdue']}); the manual nudge sent inside the grace period used it up")


# ---------------------------------------------------------------------------
# The 5-minute tick racing "Mark recurring"
# ---------------------------------------------------------------------------
async def test_a_tick_running_while_a_schedule_is_marked_does_not_bill_the_due_twice(api, other_merchant, db):
    """POST /payers/{id}/recurring commits the schedule, then creates the first due itself
    (create_due_invoices without the tick's advisory lock). If the 5-minute cron tick runs at that
    moment it sees the same schedule and creates the same due: both check "exists" before either
    inserts. The customer gets two invoices (two links, two emails) for one due."""
    m = other_merchant
    async with connection() as c:
        sched = await set_recurring(c, m["merchant_id"], m["payer_a"], cadence="weekly", amount_paise=500_00,
                                    grace_days=0, email="asha@example.com")

    async def router_step():  # recurring.py router: create_due_invoices(conn, schedule["id"], errors)
        async with connection() as c:
            return await create_due_invoices(c, sched["id"], [])

    async def cron_step():  # jobs.tick: create_due_invoices(conn), under the tick lock only
        async with connection() as c:
            return await create_due_invoices(c)

    await asyncio.gather(router_step(), cron_step())
    dues = [i for i in invoices_of(db, m["payer_a"]) if i["status"] != "cancelled"]
    by_date: dict = {}
    for i in dues:
        by_date.setdefault(i["due_date"], []).append(i["number"])
    doubled = {d: n for d, n in by_date.items() if len(n) > 1}
    invoice_emails = [e for e in email_svc.SENT if e["subject"].startswith("Invoice ")]
    assert not doubled, (f"one due billed twice: {doubled}; {len(invoice_emails)} invoice emails went to the customer, "
                         f"outstanding {sum(i['amount_paise'] for i in dues) // 100} rupees for one ₹500 due")


# ---------------------------------------------------------------------------
# Scheduler wiring (RUN_SCHEDULER=true): the loop runs the tick
# ---------------------------------------------------------------------------
async def test_the_in_process_scheduler_runs_the_tick_and_survives_a_failing_run(monkeypatch):
    calls = []

    async def fake_tick(conn):
        calls.append(conn is not None)
        if len(calls) == 1:
            raise RuntimeError("database blip")
        return {"ok": True}

    monkeypatch.setattr(jobs_svc, "INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(jobs_svc, "tick", fake_tick)
    task = asyncio.create_task(jobs_svc.scheduler_loop())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(calls) >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3 and all(calls)


def test_cron_service_posts_the_tick_every_five_minutes_with_the_secret():
    from pathlib import Path
    import json

    root = Path(__file__).resolve().parents[3] / "deploy" / "cron"
    cfg = json.loads((root / "railway.json").read_text())
    assert cfg["deploy"]["cronSchedule"] == "*/5 * * * *"
    docker = (root / "Dockerfile").read_text()
    assert "/jobs/tick" in docker and "X-Cron-Secret: $CRON_SECRET" in docker and "-X POST" in docker
