"""Tester round 4, M5: recurring schedules, dues ahead of the due date, overdue + one auto-reminder,
tracking-only, stop, the demo clock (/admin/clock), /jobs/tick (cron secret, single-flight, isolated
steps, status polling, AI descriptions), the recurring suggestion chip and identity rules on the
recurring email.

Spec: ARCHITECTURE.md moment 4 ("Recurring plus auto-reminder"), Decisions/Recurring + Identity,
Data model (recurring_schedules, reminders, app_clock), API surface (POST /payers/{id}/recurring,
/jobs/tick, /admin/clock), Low-friction ledger (AI description, recurring suggestion chip),
Collections flow ("Overdue (due + grace) triggers a reminder email with the same link. Recurring
schedules with an email create invoices per due date; without an email they only track.").

All writes use the throwaway `other_merchant`. Ticks sweep every merchant, so the demo merchant is
re-seeded after the module and the demo clock is restored after each test that moves it.
"""

from __future__ import annotations

import calendar
import json
import uuid
from datetime import date, datetime, timedelta

import psycopg
import pytest

from app.money import IST
from app.services import email as email_svc
from app.services.email import EmailError
from app.services.descriptions import generate as _ORIGINAL_GENERATE  # bound before any monkeypatching
from app.services.paytm.checksum import sign_form

from ..conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def _reseed_after_module(reseed_demo):
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def _fake_descriptions(monkeypatch):
    async def fake_generate(t):
        return {"description": "Recurring dues payment", "category": "Services"}

    monkeypatch.setattr("app.services.descriptions.generate", fake_generate)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


def cron(settings) -> dict:
    return {"X-Cron-Secret": settings.cron_secret}


async def mark(api, m, payer_key="payer_a", **body):
    payload = {"cadence": "monthly", "amount_paise": 1000_00} | body
    return await api.post(f"/payers/{m[payer_key]}/recurring", json=payload, headers=bearer(m["token"]))


async def jump(api, token, **body):
    r = await api.post("/admin/clock", json=body, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def invoices_of(db, payer_id) -> list[dict]:
    return db.execute(
        "select * from invoices where payer_id = %s order by due_date, created_at", (payer_id,)
    ).fetchall()


def reminders_of(db, invoice_id, kind=None) -> list[dict]:
    if kind:
        return db.execute("select * from reminders where invoice_id = %s and kind = %s", (invoice_id, kind)).fetchall()
    return db.execute("select * from reminders where invoice_id = %s", (invoice_id,)).fetchall()


def mails_for(number: str, prefix: str) -> list[dict]:
    return [m for m in email_svc.SENT if m["subject"].startswith(prefix) and number in m["html"]]


# ---------------------------------------------------------------------------
# Marking recurring
# ---------------------------------------------------------------------------
async def test_weekly_with_email_creates_the_first_due_with_link_qr_and_email(api, other_merchant, db, restore_clock):
    email_svc.SENT.clear()
    d0 = today(db)
    r = await mark(api, other_merchant, cadence="weekly", amount_paise=250_00, email="asha@example.com")
    assert r.status_code == 201, r.text
    body = r.json()
    s = body["schedule"]
    assert s["cadence"] == "weekly" and s["amount_paise"] == 250_00 and s["grace_days"] == 3
    assert s["anchor_date"] == d0.isoformat() and s["active"] and s["auto_remind"] and not s["tracking_only"]
    assert s["merchant_id"] == str(other_merchant["merchant_id"])
    [due] = body["dues_created"]
    assert due["emailed"] and due["due_date"] == d0.isoformat()
    [inv] = invoices_of(db, other_merchant["payer_a"])
    assert inv["recurring_id"] == uuid.UUID(s["id"]) and inv["due_date"] == d0 and inv["status"] == "sent"
    assert inv["amount_paise"] == 250_00 and inv["grace_days"] == 3
    assert inv["link_url"].endswith(f"/mock-pay/{inv['paytm_order_id']}") and inv["qr_image_url"]
    assert "Weekly payment" in inv["line_items"][0]["name"]
    [mail] = mails_for(inv["number"], "Invoice ")
    assert mail["intended_to"] == "asha@example.com" and inv["link_url"] in mail["html"]
    nxt = db.execute("select next_due_date from recurring_schedules where id = %s", (s["id"],)).fetchone()
    assert nxt["next_due_date"] == d0 + timedelta(days=7)
    assert "Recurring" in body["payer"]["badges"] and body["payer"]["recurring_schedule"]["cadence"] == "weekly"


async def test_due_is_created_and_emailed_three_days_ahead_not_earlier(api, other_merchant, db, restore_clock):
    d0 = today(db)
    anchor = d0 + timedelta(days=5)
    r = await mark(api, other_merchant, anchor_date=anchor.isoformat(), email="asha@example.com")
    assert r.status_code == 201 and r.json()["dues_created"] == []
    out = await jump(api, other_merchant["token"], days=1)  # due in 4 days: still nothing
    assert invoices_of(db, other_merchant["payer_a"]) == []
    email_svc.SENT.clear()
    out = await jump(api, other_merchant["token"], days=1)  # due in 3 days: created and emailed
    [inv] = invoices_of(db, other_merchant["payer_a"])
    assert inv["due_date"] == anchor and inv["status"] == "sent"
    assert any(d["invoice"] == inv["number"] and d["emailed"] for d in out["jobs"]["dues_created"])
    assert len(mails_for(inv["number"], "Invoice ")) == 1


async def test_overdue_only_after_due_plus_grace_and_exactly_one_reminder(api, other_merchant, db, restore_clock):
    await mark(api, other_merchant, grace_days=2, email="asha@example.com")
    [inv] = invoices_of(db, other_merchant["payer_a"])
    email_svc.SENT.clear()
    await jump(api, other_merchant["token"], days=2)  # due + grace = today: still in grace
    assert db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()["status"] == "sent"
    out = await jump(api, other_merchant["token"], days=1)  # past due + grace
    assert db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()["status"] == "overdue"
    assert inv["number"] in out["jobs"]["overdue"]["overdue"] and inv["number"] in out["jobs"]["overdue"]["reminded"]
    for _ in range(2):
        again = await jump(api, other_merchant["token"], days=1)
        assert inv["number"] not in again["jobs"]["overdue"]["reminded"]
    assert len(reminders_of(db, inv["id"], "overdue")) == 1
    [mail] = mails_for(inv["number"], "Reminder:")
    assert mail["intended_to"] == "asha@example.com" and inv["link_url"] in mail["html"]  # same link


async def test_auto_remind_off_flags_overdue_without_a_reminder(api, other_merchant, db, restore_clock):
    r = await mark(api, other_merchant, grace_days=0, email="asha@example.com", auto_remind=False)
    s = r.json()["schedule"]
    assert s["auto_remind"] is False and not s["tracking_only"] and r.json()["dues_created"][0]["emailed"]
    [inv] = invoices_of(db, other_merchant["payer_a"])
    out = await jump(api, other_merchant["token"], days=1)
    assert inv["number"] in out["jobs"]["overdue"]["overdue"] and inv["number"] not in out["jobs"]["overdue"]["reminded"]
    assert reminders_of(db, inv["id"], "overdue") == []


async def test_tracking_only_without_email_never_sends_anything(api, other_merchant, db, restore_clock):
    email_svc.SENT.clear()
    r = await mark(api, other_merchant, payer_key="payer_b", cadence="weekly", amount_paise=200_00, grace_days=1)
    body = r.json()
    assert body["schedule"]["tracking_only"] and body["schedule"]["auto_remind"] is False
    await jump(api, other_merchant["token"], days=8)  # first due (D, grace 1) is missed; D+7 is still in grace
    invs = invoices_of(db, other_merchant["payer_b"])
    assert len(invs) == 2 and invs[0]["status"] == "overdue" and invs[1]["status"] == "sent"
    assert all(i["link_url"] and i["email_to"] is None for i in invs)  # payable, never emailed
    assert all(reminders_of(db, i["id"]) == [] for i in invs)
    assert not [m for m in email_svc.SENT for i in invs if i["number"] in m["html"]]


async def test_paid_or_cancelled_dues_never_go_overdue_or_get_reminded(api, other_merchant, db, settings, restore_clock):
    await mark(api, other_merchant, cadence="weekly", grace_days=0, email="asha@example.com")
    [inv] = invoices_of(db, other_merchant["payer_a"])
    now = datetime.now(IST)
    fields = {
        "ORDERID": inv["paytm_order_id"], "MID": settings.paytm_mid_active, "TXNID": "R4" + uuid.uuid4().hex[:20],
        "TXNAMOUNT": f"{inv['amount_paise'] / 100:.2f}", "CURRENCY": "INR", "PAYMENTMODE": "UPI",
        "TXNDATE": now.strftime("%Y-%m-%d"), "TXNDATETIME": now.strftime("%Y-%m-%d %H:%M:%S.0"),
        "STATUS": "TXN_SUCCESS", "RESPCODE": "01", "RESPMSG": "Txn Success", "MERC_UNQ_REF": inv["link_id"],
    }
    fields["CHECKSUMHASH"] = sign_form(fields, settings.paytm_key)
    assert (await api.post("/webhooks/paytm/link", data=fields)).json()["result"] == "paid"
    await jump(api, other_merchant["token"], days=5)  # creates the next weekly due
    invs = invoices_of(db, other_merchant["payer_a"])
    nxt = invs[1]
    assert (await api.post(f"/invoices/{nxt['id']}/cancel", headers=bearer(other_merchant["token"]))).status_code == 200
    out = await jump(api, other_merchant["token"], days=5)
    statuses = {i["number"]: i["status"] for i in invoices_of(db, other_merchant["payer_a"])}
    assert statuses[inv["number"]] == "paid" and statuses[nxt["number"]] == "cancelled"
    assert inv["number"] not in out["jobs"]["overdue"]["overdue"] and nxt["number"] not in out["jobs"]["overdue"]["overdue"]
    assert reminders_of(db, inv["id"], "overdue") == [] and reminders_of(db, nxt["id"], "overdue") == []


async def test_stop_recurring_stops_new_dues_and_keeps_existing_ones(api, other_merchant, db, restore_clock):
    h = bearer(other_merchant["token"])
    await mark(api, other_merchant, cadence="weekly", email="asha@example.com")
    [inv] = invoices_of(db, other_merchant["payer_a"])
    assert (await api.delete(f"/payers/{other_merchant['payer_a']}/recurring", headers=h)).json() == {"stopped": True}
    await jump(api, other_merchant["token"], days=21)
    invs = invoices_of(db, other_merchant["payer_a"])
    assert [i["id"] for i in invs] == [inv["id"]]  # no new dues after stopping
    p = (await api.get(f"/payers/{other_merchant['payer_a']}", headers=h)).json()
    assert p["recurring_schedule"] is None and "Recurring" not in p["badges"]


async def test_monthly_dues_catch_up_after_a_big_jump(api, other_merchant, db, restore_clock):
    d0 = today(db)
    await mark(api, other_merchant, email="asha@example.com")
    await jump(api, other_merchant["token"], days=70)
    dues = [i["due_date"] for i in invoices_of(db, other_merchant["payer_a"])]
    assert len(dues) == len(set(dues)) >= 3
    assert all(d.day == min(d0.day, calendar.monthrange(d.year, d.month)[1]) for d in dues)
    assert dues[0] == d0
    t = today(db)
    nxt = db.execute("select next_due_date from recurring_schedules where payer_id = %s and active",
                     (other_merchant["payer_a"],)).fetchone()["next_due_date"]
    assert nxt > t + timedelta(days=3) and dues[-1] <= t + timedelta(days=3)


async def test_weekly_catch_up_after_a_two_month_jump_has_no_gaps_or_duplicates(api, other_merchant, db, settings,
                                                                               restore_clock):
    await mark(api, other_merchant, payer_key="payer_b", cadence="weekly", amount_paise=200_00)
    await jump(api, other_merchant["token"], days=60)
    await api.post("/jobs/tick", headers=cron(settings))  # the next cron firing finishes any backlog
    await api.post("/jobs/tick", headers=cron(settings))
    dues = [i["due_date"] for i in invoices_of(db, other_merchant["payer_b"])]
    gaps = {(b - a).days for a, b in zip(dues, dues[1:])}
    assert gaps == {7} and len(dues) == len(set(dues))
    assert dues[-1] > today(db) - timedelta(days=7)


# ---------------------------------------------------------------------------
# Identity rules on the email given when marking recurring
# ---------------------------------------------------------------------------
async def test_new_email_is_normalised_saved_and_becomes_an_identifier(api, other_merchant, db, restore_clock):
    email_svc.SENT.clear()
    r = await mark(api, other_merchant, payer_key="payer_b", email="  New.Person@Example.COM ")
    assert r.status_code == 201, r.text
    p = db.execute("select email from payers where id = %s", (other_merchant["payer_b"],)).fetchone()
    assert p["email"] == "new.person@example.com"
    ident = db.execute("select 1 from payer_identifiers where payer_id = %s and kind = 'email' and value = %s",
                       (other_merchant["payer_b"], "new.person@example.com")).fetchone()
    assert ident
    [inv] = invoices_of(db, other_merchant["payer_b"])
    assert inv["email_to"] == "new.person@example.com"
    assert email_svc.SENT[-1]["intended_to"] == "new.person@example.com"


async def test_email_owned_by_another_customer_is_refused_without_side_effects(api, other_merchant, db):
    db.execute("insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s, %s, 'email', "
               "'asha@example.com')", (other_merchant["payer_a"], other_merchant["merchant_id"]))
    r = await mark(api, other_merchant, payer_key="payer_b", email="ASHA@example.com")
    assert r.status_code == 409 and "Asha Test" in r.json()["detail"]
    assert db.execute("select count(*) as n from recurring_schedules where payer_id = %s",
                      (other_merchant["payer_b"],)).fetchone()["n"] == 0
    assert db.execute("select email from payers where id = %s", (other_merchant["payer_b"],)).fetchone()["email"] is None
    assert invoices_of(db, other_merchant["payer_b"]) == []


async def test_email_typed_when_marking_recurring_is_where_dues_go(api, other_merchant, db, restore_clock):
    """Decisions/Recurring: "An optional email popup appears when marking recurring." The popup
    (recurring-dialog.tsx) is pre-filled with the payer's email and labelled "Email for invoices and
    reminders". When the merchant types a different address there, the dues must go to it."""
    email_svc.SENT.clear()
    r = await mark(api, other_merchant, email="asha.accounts@example.com")
    assert r.status_code == 201, r.text
    [inv] = invoices_of(db, other_merchant["payer_a"])
    sent_to = [m["intended_to"] for m in mails_for(inv["number"], "Invoice ")]
    assert inv["email_to"] == "asha.accounts@example.com" and sent_to == ["asha.accounts@example.com"], (
        f"recurring email ignored: invoice email_to={inv['email_to']!r}, emailed to {sent_to}")


# ---------------------------------------------------------------------------
# Re-marking, merging, ownership, validation
# ---------------------------------------------------------------------------
async def test_re_marking_recurring_does_not_double_bill_the_same_due(api, other_merchant, db, restore_clock):
    """POST /payers/{id}/recurring on a payer that is already recurring replaces the schedule
    (set_recurring deactivates the old one). The customer must not end up with two open invoices
    for the same due date."""
    await mark(api, other_merchant, amount_paise=1000_00, email="asha@example.com")
    r = await mark(api, other_merchant, amount_paise=1200_00, email="asha@example.com")  # e.g. a new amount
    assert r.status_code == 201, r.text
    active = db.execute("select count(*) as n from recurring_schedules where payer_id = %s and active",
                        (other_merchant["payer_a"],)).fetchone()["n"]
    assert active == 1
    open_today = db.execute(
        "select number, amount_paise from invoices where payer_id = %s and due_date = app_today() "
        "and status in ('sent', 'overdue') order by number", (other_merchant["payer_a"],)).fetchall()
    assert len(open_today) == 1, f"two open dues for the same date: {open_today}"


async def test_schedule_follows_a_merge(api, other_merchant, db, restore_clock):
    h = bearer(other_merchant["token"])
    await mark(api, other_merchant, payer_key="payer_b", cadence="weekly", amount_paise=200_00)
    r = await api.post(f"/payers/{other_merchant['payer_a']}/merge", json={"other_payer_id": str(other_merchant["payer_b"])},
                       headers=h)
    assert r.status_code == 200, r.text
    await jump(api, other_merchant["token"], days=7)
    s = db.execute("select payer_id from recurring_schedules where active and merchant_id = %s",
                   (other_merchant["merchant_id"],)).fetchone()
    assert s["payer_id"] == other_merchant["payer_a"]
    assert len(invoices_of(db, other_merchant["payer_a"])) == 2  # the moved due + the new one


async def test_marking_a_merged_payer_is_404(api, other_merchant, db):
    h = bearer(other_merchant["token"])
    await api.post(f"/payers/{other_merchant['payer_a']}/merge", json={"other_payer_id": str(other_merchant["payer_b"])},
                   headers=h)
    r = await mark(api, other_merchant, payer_key="payer_b")
    assert r.status_code == 404


async def test_recurring_routes_are_scoped_and_authenticated(api, other_merchant, demo_token, db, seeded):
    ram = db.execute("select id from payers where merchant_id = %s and display_name = 'Ram'",
                     (seeded["merchant_id"],)).fetchone()["id"]
    h = bearer(other_merchant["token"])
    assert (await api.post(f"/payers/{ram}/recurring", json={"cadence": "weekly", "amount_paise": 100_00},
                           headers=h)).status_code == 404
    assert (await api.delete(f"/payers/{ram}/recurring", headers=h)).status_code == 404
    assert (await api.post(f"/payers/{ram}/recurring", json={"cadence": "weekly", "amount_paise": 100_00})).status_code == 401
    assert (await api.delete(f"/payers/{ram}/recurring")).status_code == 401
    assert (await api.post(f"/payers/{ram}/recurring", json={"cadence": "weekly", "amount_paise": 100_00},
                           headers=bearer("x.y.z"))).status_code == 401
    # The demo merchant's other schedules are untouched by another merchant's stop.
    assert db.execute("select count(*) as n from recurring_schedules where payer_id = %s", (ram,)).fetchone()["n"] == 0


@pytest.mark.parametrize("body", [
    {"cadence": "fortnightly", "amount_paise": 100_00},
    {"cadence": "weekly", "amount_paise": 99},
    {"cadence": "weekly", "amount_paise": 100_00, "grace_days": -1},
    {"cadence": "weekly", "amount_paise": 100_00, "grace_days": 61},
    {"cadence": "weekly", "amount_paise": 100_00, "anchor_date": "not-a-date"},
    {"cadence": "weekly", "amount_paise": 100_00, "email": "nope@"},
    {"amount_paise": 100_00},
])
async def test_recurring_body_validation(api, other_merchant, body):
    r = await api.post(f"/payers/{other_merchant['payer_a']}/recurring", json=body,
                       headers=bearer(other_merchant["token"]))
    assert r.status_code == 422, r.text


async def test_first_due_before_the_app_clock_today_is_refused(api, other_merchant, db):
    r = await mark(api, other_merchant, anchor_date=(today(db) - timedelta(days=1)).isoformat())
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Overdue reminder delivery
# ---------------------------------------------------------------------------
async def test_a_failed_overdue_reminder_is_retried_by_the_next_tick(api, other_merchant, db, settings, restore_clock,
                                                                     monkeypatch):
    """Moment 4: "a missed due triggers a reminder email". If Resend fails once while the due goes
    overdue, the next 5-minute tick must still send that one reminder."""
    import app.services.invoices as inv_svc

    await mark(api, other_merchant, grace_days=0, email="asha@example.com")
    [inv] = invoices_of(db, other_merchant["payer_a"])
    real_send = inv_svc.send_email
    state = {"failed": False}

    async def flaky_send(to, subject, body, attachments=None):
        if to == "asha@example.com" and subject.startswith("Reminder:") and not state["failed"]:
            state["failed"] = True
            raise EmailError("Resend rejected the email (503): try again")
        return await real_send(to, subject, body, attachments)

    monkeypatch.setattr(inv_svc, "send_email", flaky_send)
    out = await jump(api, other_merchant["token"], days=1)
    assert state["failed"] and inv["number"] in out["jobs"]["overdue"]["overdue"]
    for _ in range(2):  # two more cron firings
        await api.post("/jobs/tick", headers=cron(settings))
    got = reminders_of(db, inv["id"], "overdue")
    assert len(got) == 1, f"missed due {inv['number']} got {len(got)} overdue reminders after a transient email failure"


# ---------------------------------------------------------------------------
# Demo clock
# ---------------------------------------------------------------------------
async def test_admin_clock_jump_set_reset_and_validation(api, other_merchant, restore_clock):
    tok = other_merchant["token"]
    before = (await api.get("/admin/clock", headers=bearer(tok))).json()
    wall_start = datetime.now(IST)
    j = await jump(api, tok, hours=6, run_jobs=False)
    delta = datetime.fromisoformat(j["now"]) - datetime.fromisoformat(before["now"])
    assert timedelta(hours=5, minutes=59) < delta < timedelta(hours=6, minutes=2) and "jobs" not in j
    target = datetime(2027, 3, 15, 9, 0, tzinfo=IST)
    s = await jump(api, tok, set_to=target.isoformat(), run_jobs=False)
    assert abs(datetime.fromisoformat(s["now"]) - target) < timedelta(seconds=30) and s["overridden"]
    me = (await api.get("/me", headers=bearer(tok))).json()
    assert me["clock_overridden"] and abs(datetime.fromisoformat(me["now"]) - target) < timedelta(seconds=30)
    with_jobs = await jump(api, tok, days=1)
    assert {"dues_created", "overdue", "paid_by_status_poll", "descriptions_written", "alerts"} <= set(with_jobs["jobs"])
    r = await jump(api, tok, reset=True, run_jobs=False)
    # Reset returns to where the seed put the clock (which keeps running), not to wall time.
    expected = datetime.fromisoformat(before["now"]) + (datetime.now(IST) - wall_start)
    assert not r["overridden"] and abs(datetime.fromisoformat(r["now"]) - expected) < timedelta(seconds=30)
    for bad in ({}, {"days": 366}, {"hours": 24 * 366}, {"days": "soon"}, {"set_to": "tomorrow"}):
        assert (await api.post("/admin/clock", json=bad, headers=bearer(tok))).status_code == 422, bad


async def test_admin_clock_needs_a_valid_token(api):
    assert (await api.get("/admin/clock")).status_code == 401
    assert (await api.post("/admin/clock", json={"days": 1})).status_code == 401
    assert (await api.post("/admin/clock", json={"days": 1}, headers=bearer("abc.def.ghi"))).status_code == 401


async def test_admin_clock_is_off_outside_mock_mode(api, other_merchant, settings, monkeypatch, db, restore_clock):
    before = db.execute("select override_now from app_clock where id = 1").fetchone()["override_now"]
    monkeypatch.setattr(settings, "paytm_mode", "staging")
    h = bearer(other_merchant["token"])
    assert (await api.get("/admin/clock", headers=h)).status_code == 404
    assert (await api.post("/admin/clock", json={"days": 3}, headers=h)).status_code == 404
    assert (await api.post("/admin/clock", json={"reset": True}, headers=h)).status_code == 404
    assert db.execute("select override_now from app_clock where id = 1").fetchone()["override_now"] == before
    assert (await api.get("/me", headers=h)).json()["demo_tools"] is False


# ---------------------------------------------------------------------------
# /jobs/tick
# ---------------------------------------------------------------------------
async def test_tick_needs_the_cron_secret_not_a_user_token(api, settings, other_merchant):
    assert (await api.post("/jobs/tick", headers=bearer(other_merchant["token"]))).status_code == 401
    assert (await api.post("/jobs/tick", headers={"X-Cron-Secret": ""})).status_code == 401
    assert (await api.post("/jobs/tick", headers={"X-Cron-Secret": settings.cron_secret + "x"})).status_code == 401


async def test_tick_with_no_configured_secret_refuses_everyone(api, settings, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "")
    assert (await api.post("/jobs/tick", headers={"X-Cron-Secret": ""})).status_code == 401


async def test_a_failing_step_does_not_block_the_other_steps_and_releases_the_lock(api, other_merchant, db, settings,
                                                                                   monkeypatch):
    async def broken(conn, schedule_id=None):
        await conn.execute("select 1 / 0")  # a real database error on the shared connection

    monkeypatch.setattr("app.services.recurring.create_due_invoices", broken)
    t = db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source) "
                   "values (%s, %s, 4500, app_now(), 'mock') returning id",
                   (other_merchant["merchant_id"], other_merchant["payer_a"])).fetchone()["id"]
    r = await api.post("/jobs/tick", headers=cron(settings))
    assert r.status_code == 200
    out = r.json()
    assert "error" in out["dues_created"]
    assert "error" not in out["overdue"] and isinstance(out["paid_by_status_poll"], list)
    assert out["descriptions_written"] >= 1 and isinstance(out["alerts"], list)
    row = db.execute("select description, description_by from transactions where id = %s", (t,)).fetchone()
    assert row == {"description": "Recurring dues payment", "description_by": "ai"}
    again = (await api.post("/jobs/tick", headers=cron(settings))).json()
    assert "skipped" not in again  # the advisory lock was released


async def test_tick_while_another_runs_is_skipped_and_clock_jump_says_so(api, other_merchant, settings, restore_clock):
    with psycopg.connect(settings.database_url, autocommit=True) as other:
        other.execute("select pg_advisory_lock(4242001)")
        try:
            r = await api.post("/admin/clock", json={"hours": 1}, headers=bearer(other_merchant["token"]))
            assert r.status_code == 200 and r.json()["jobs"] == {"skipped": "another tick is running"}
        finally:
            other.execute("select pg_advisory_unlock(4242001)")


async def test_tick_polls_paytm_status_for_open_dues_older_than_two_minutes(api, other_merchant, db, restore_clock):
    await mark(api, other_merchant, email="asha@example.com")
    [inv] = invoices_of(db, other_merchant["payer_a"])
    paid = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})
    assert paid.status_code == 200 and paid.json()["webhook_sent"] is False
    out = await jump(api, other_merchant["token"], hours=0.01)  # under 2 minutes old: not polled yet
    assert inv["number"] not in [p["invoice"] for p in out["jobs"]["paid_by_status_poll"]]
    out = await jump(api, other_merchant["token"], hours=0.05)
    assert inv["number"] in [p["invoice"] for p in out["jobs"]["paid_by_status_poll"]]
    row = db.execute("select status from invoices where id = %s", (inv["id"],)).fetchone()
    assert row["status"] == "paid"
    [t] = db.execute("select paytm_txn_id from transactions where invoice_id = %s", (inv["id"],)).fetchall()
    assert t["paytm_txn_id"] == paid.json()["txn_id"]


async def test_ai_description_never_overwrites_a_merchant_edit_made_mid_generation(api, other_merchant, db, settings,
                                                                                  monkeypatch):
    h = bearer(other_merchant["token"])
    t = db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source) "
                   "values (%s, %s, 75000, app_now(), 'mock') returning id",
                   (other_merchant["merchant_id"], other_merchant["payer_a"])).fetchone()["id"]

    async def slow_generate(ctx):
        if ctx["id"] == t:  # the merchant edits while Haiku is thinking
            r = await api.patch(f"/transactions/{t}", json={"description": "Wedding order advance"}, headers=h)
            assert r.status_code == 200
        return {"description": "Groceries for the week", "category": "Groceries"}

    monkeypatch.setattr("app.services.descriptions.generate", slow_generate)
    await api.post("/jobs/tick", headers=cron(settings))
    row = db.execute("select description, description_by, category from transactions where id = %s", (t,)).fetchone()
    assert row["description"] == "Wedding order advance" and row["description_by"] == "merchant"


async def test_ai_description_prompt_and_eight_word_cap(other_merchant, db, settings, monkeypatch):
    """Haiku gets amount, time, link/invoice notes and the payer's last 5 descriptions; output ≤ 8 words."""
    import app.services.descriptions as d
    from app.db import connection

    db.execute("delete from transactions where merchant_id = %s", (other_merchant["merchant_id"],))
    for i in range(6):
        db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source, description) "
                   "values (%s, %s, 10000, app_now() - %s * interval '1 day', 'seed', %s)",
                   (other_merchant["merchant_id"], other_merchant["payer_a"], i + 1, f"History item {i}"))
    t = db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source, raw) "
                   "values (%s, %s, 123400, app_now(), 'mock', %s) returning id",
                   (other_merchant["merchant_id"], other_merchant["payer_a"],
                    json.dumps({"LINKDESCRIPTION": "Invoice INV0099"}))).fetchone()["id"]
    seen = {}

    class Block:
        type = "text"
        text = json.dumps({"description": "One two three four five six seven eight nine ten.",
                           "category": "Groceries"})

    class Resp:
        stop_reason = "end_turn"
        content = [Block()]

    class Messages:
        async def create(self, **kw):
            seen.update(kw)
            return Resp()

    class Client:
        messages = Messages()

    monkeypatch.setattr(d, "client", lambda: Client())
    monkeypatch.setattr(settings, "anthropic_api_key", settings.anthropic_api_key or "test-key")
    async with connection() as conn:
        ctx = await d._context(conn, t)
    out = await _ORIGINAL_GENERATE(ctx)  # the real function (the autouse fixture swaps the module attribute)
    prompt = seen["messages"][0]["content"]
    assert "₹1,234" in prompt and "Invoice INV0099" in prompt and "IST" in prompt
    assert sum(f"History item {i}" in prompt for i in range(6)) == 5
    assert out == {"description": "One two three four five six seven eight", "category": "Groceries"}
    assert seen["model"] == settings.agent_model




async def test_paid_invoice_payment_gets_an_ai_description_on_the_next_tick(api, other_merchant, db, settings):
    h = bearer(other_merchant["token"])
    r = await api.post("/invoices", headers=h, json={
        "payer_id": str(other_merchant["payer_b"]), "due_date": (today(db) + timedelta(days=3)).isoformat(),
        "line_items": [{"name": "Rice 25kg", "qty": 1, "unit_price_paise": 1800_00}]})
    inv = r.json()
    paid = await api.post(f"/mock-pay/{inv['paytm_order_id']}", json={"outcome": "success_no_webhook"})
    assert (await api.post(f"/invoices/{inv['id']}/check", headers=h)).json()["status"] == "paid"
    [t] = db.execute("select id, description from transactions where invoice_id = %s", (inv["id"],)).fetchall()
    assert t["description"] is None and paid.status_code == 200
    await api.post("/jobs/tick", headers=cron(settings))
    row = db.execute("select description, description_by from transactions where id = %s", (t["id"],)).fetchone()
    assert row == {"description": "Recurring dues payment", "description_by": "ai"}


# ---------------------------------------------------------------------------
# Recurring suggestion chip
# ---------------------------------------------------------------------------
async def test_suggestion_for_steady_weekly_and_daily_payers_but_not_irregular_or_recurring(api, other_merchant, db,
                                                                                            restore_clock):
    m, a, b = other_merchant["merchant_id"], other_merchant["payer_a"], other_merchant["payer_b"]
    h = bearer(other_merchant["token"])
    db.execute("delete from transactions where merchant_id = %s", (m,))
    for i in range(8):
        db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source) "
                   "values (%s, %s, 50000, app_now() - %s * interval '7 days', 'seed')", (m, a, i + 1))
        db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source) "
                   "values (%s, %s, %s, app_now() - %s * interval '7 days', 'seed')", (m, b, 10000 + i * 3700, i + 1))
    sa = (await api.get(f"/payers/{a}", headers=h)).json()["recurring_suggestion"]
    assert sa and sa["cadence"] == "weekly" and sa["amount_paise"] == 50000 and sa["every_days"] == 7
    assert (await api.get(f"/payers/{b}", headers=h)).json()["recurring_suggestion"] is None
    await mark(api, other_merchant, cadence="weekly", amount_paise=50000)
    assert (await api.get(f"/payers/{a}", headers=h)).json()["recurring_suggestion"] is None


async def test_seeded_ram_gets_a_daily_suggestion(api, demo_token, db, seeded):
    ram = db.execute("select id from payers where merchant_id = %s and display_name = 'Ram'",
                     (seeded["merchant_id"],)).fetchone()["id"]
    s = (await api.get(f"/payers/{ram}", headers=bearer(demo_token))).json()["recurring_suggestion"]
    assert s and s["every_days"] == 1 and s["amount_paise"] == 10000


async def test_adding_an_email_later_turns_on_invoices_and_reminders(api, other_merchant, db, restore_clock):
    """The tracking-only note promises: "Add an email any time to send invoices and reminders."
    (recurring-dialog.tsx). After the merchant adds an email, the next missed due must get its
    reminder, and the schedule must stop claiming it is tracking only."""
    h = bearer(other_merchant["token"])
    r = await mark(api, other_merchant, payer_key="payer_b", cadence="weekly", amount_paise=300_00, grace_days=0)
    assert r.json()["schedule"]["tracking_only"]
    # Emails for a schedule are set on the schedule itself ("Edit schedule" in the UI).
    p = await mark(api, other_merchant, payer_key="payer_b", cadence="weekly", amount_paise=300_00, grace_days=0,
                   email="asha.t@example.com")
    assert p.status_code == 201, p.text
    email_svc.SENT.clear()
    await jump(api, other_merchant["token"], days=4)  # D+7 due created and emailed 3 days ahead
    out = await jump(api, other_merchant["token"], days=4)  # then missed
    invs = invoices_of(db, other_merchant["payer_b"])
    second = invs[1]
    assert second["email_to"] == "asha.t@example.com" and mails_for(second["number"], "Invoice ")  # emailed
    sched = (await api.get(f"/payers/{other_merchant['payer_b']}", headers=h)).json()["recurring_schedule"]
    reminded = reminders_of(db, second["id"], "overdue")
    assert second["number"] in out["jobs"]["overdue"]["overdue"]
    assert len(reminded) == 1 and sched["auto_remind"], (
        f"email added later: due {second['number']} was emailed but its missed due got {len(reminded)} reminders; "
        f"schedule auto_remind={sched['auto_remind']} (card shows 'Tracking only ... no emails')")
