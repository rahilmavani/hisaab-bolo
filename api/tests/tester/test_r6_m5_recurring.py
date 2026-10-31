"""Tester round 6, M5: stopping and re-marking a schedule, edits that change grace/cadence/email,
refused requests, Storage failures while a due is created, monthly due dates on the 29th-31st.

Spec: ARCHITECTURE.md moment 4 ("the merchant marks a payer recurring, and a missed due triggers a
reminder email"), Decisions/Recurring ("Track, auto-create dues and auto-remind. An optional email
popup appears when marking recurring. With no email, the schedule only tracks"), Collections flow
(steps 2-3: link, QR PNG to Supabase Storage, email; "Recurring schedules with an email create
invoices per due date"), API surface (POST /payers/{id}/recurring, /jobs/tick, /admin/clock).
UI copy that states the intended behaviour:
  recurring-card.tsx    "Invoices and overdue reminders go to {email}." / "Next due {d}, {n} days grace"
  recurring-dialog.tsx  "If it's still unpaid {grace} days after the due date, a reminder goes out the
                         following day." / "Schedule saved, but the first invoice couldn't be created
                         (...). It will be tried again automatically." / "{INV} for {d} is already invoiced."

Writes use the throwaway `other_merchant`; clock jumps run the jobs for every merchant, so the demo
merchant is re-seeded after the module and the clock is restored after each test that moves it.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from app.services import email as email_svc
from app.services import invoices as inv_svc
from app.services.recurring import next_due

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


def cron(settings) -> dict:
    return {"X-Cron-Secret": settings.cron_secret}


async def mark(api, m, payer_key="payer_a", expect=201, **body):
    payload = {"cadence": "weekly", "amount_paise": 800_00, "grace_days": 0} | body
    r = await api.post(f"/payers/{m[payer_key]}/recurring", json=payload, headers=bearer(m["token"]))
    assert r.status_code == expect, r.text
    return r.json()


async def stop(api, m, payer_key="payer_a"):
    r = await api.delete(f"/payers/{m[payer_key]}/recurring", headers=bearer(m["token"]))
    assert r.status_code == 200, r.text


async def jump(api, token, **body) -> dict:
    r = await api.post("/admin/clock", json=body, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def invoice_by_number(db, merchant_id, number) -> dict:
    return db.execute("select * from invoices where merchant_id = %s and number = %s", (merchant_id, number)).fetchone()


def invoices_of(db, payer_id) -> list[dict]:
    return db.execute("select * from invoices where payer_id = %s order by due_date, created_at", (payer_id,)).fetchall()


def reminders_of(db, invoice_id, kind=None) -> list[dict]:
    sql = "select * from reminders where invoice_id = %s" + (" and kind = %s" if kind else "")
    return db.execute(sql, (invoice_id, kind) if kind else (invoice_id,)).fetchall()


def schedule_of(db, payer_id) -> dict | None:
    return db.execute("select * from recurring_schedules where payer_id = %s and active", (payer_id,)).fetchone()


# ---------------------------------------------------------------------------
# Stop, then mark again
# ---------------------------------------------------------------------------
async def test_re_marked_schedule_reminds_the_missed_due_it_reports_as_already_invoiced(api, other_merchant, db,
                                                                                         restore_clock):
    """Mark weekly with an email (due today is invoiced and emailed), Stop, then Mark recurring again
    with the same email and date. The dialog says "{INV} for {d} is already invoiced." and the card
    says "Invoices and overdue reminders go to asha@example.com.", so when that due is missed the
    customer must get the reminder (moment 4)."""
    m = other_merchant
    first = await mark(api, m, email="asha@example.com")
    [due] = first["dues_created"]
    assert due["emailed"] is True
    await stop(api, m)

    again = await mark(api, m, email="asha@example.com")
    assert again["dues_created"] == []
    assert again["already_invoiced"]["invoice"] == due["invoice"]
    assert again["schedule"]["auto_remind"] is True and again["schedule"]["email"] == "asha@example.com"

    await jump(api, m["token"], days=2)  # grace 0: the due is two days past
    inv = invoice_by_number(db, m["merchant_id"], due["invoice"])
    assert inv["status"] == "overdue"
    assert len(reminders_of(db, inv["id"], "overdue")) == 1, (
        "the active schedule (email + auto-remind) covers this due, but the missed due got no reminder")


async def test_a_due_tracked_before_stop_is_emailed_after_re_marking_with_an_email(api, other_merchant, db, settings,
                                                                                   restore_clock):
    """Tracking-only schedule creates the due (no email). Stop, then mark again with an email for the
    same date: the card now promises "Invoices and overdue reminders go to {email}.", and an edit that
    adds an email does email the open due (send_unsent_due_emails). Re-marking must do the same."""
    m = other_merchant
    d = today(db) + timedelta(days=2)
    first = await mark(api, m, anchor_date=d.isoformat())  # no email -> tracking only
    [due] = first["dues_created"]
    await stop(api, m)
    again = await mark(api, m, anchor_date=d.isoformat(), email="asha@example.com")
    assert again["already_invoiced"]["invoice"] == due["invoice"]

    r = await api.post("/jobs/tick", headers=cron(settings))
    assert r.status_code == 200, r.text
    inv = invoice_by_number(db, m["merchant_id"], due["invoice"])
    sent = [x for x in email_svc.SENT if due["invoice"] in x["html"]]
    assert inv["email_to"] == "asha@example.com" and sent, (
        f"the open due {due['invoice']} is never emailed (email_to={inv['email_to']!r}, sent_at={inv['sent_at']})")


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------
async def test_more_grace_given_in_an_edit_covers_the_due_already_billed(api, other_merchant, db, restore_clock):
    """The edit dialog says "If it's still unpaid {grace} days after the due date, a reminder goes out
    the following day", and the card then shows "{grace} days grace". A merchant who gives a customer
    5 days grace on the due-day must not see that customer reminded two days later."""
    m = other_merchant
    first = await mark(api, m, email="asha@example.com", grace_days=0)
    [due] = first["dues_created"]
    s = schedule_of(db, m["payer_a"])
    edited = await mark(api, m, email="asha@example.com", grace_days=5, anchor_date=s["next_due_date"].isoformat())
    assert edited["schedule"]["grace_days"] == 5 and edited["schedule"]["updated"] is True

    await jump(api, m["token"], days=2)
    inv = invoice_by_number(db, m["merchant_id"], due["invoice"])
    assert (inv["status"], inv["grace_days"], len(reminders_of(db, inv["id"], "overdue"))) == ("sent", 5, 0), (
        f"due {due['invoice']} kept grace {inv['grace_days']} and was reminded inside the 5 days the merchant gave")


async def test_an_amount_or_email_edit_is_confirmed_as_an_update_not_as_an_old_due(api, other_merchant, db,
                                                                                  restore_clock):
    """recurring-dialog.tsx picks its note from the response: dues_created -> "{INV} for {d} created",
    else already_invoiced -> "{INV} for {d} is already invoiced.", else updated -> "Schedule updated."
    Editing the amount/email of a schedule (its Next due untouched) creates nothing; the response points
    at the first due (the kept anchor) from weeks ago, so every edit is confirmed with an unrelated
    "INV-0001 for {first due} is already invoiced." (seen in Chrome: evidence/m5/r6-chrome-04-*)."""
    m = other_merchant
    await mark(api, m, email="asha@example.com")
    s = schedule_of(db, m["payer_a"])
    edited = await mark(api, m, email="asha.accounts@example.com", amount_paise=900_00,
                        anchor_date=s["next_due_date"].isoformat())
    assert edited["schedule"]["updated"] is True and edited["dues_created"] == []
    stale = edited["already_invoiced"]
    assert stale is None or stale["due_date"] == s["next_due_date"].isoformat(), (
        f"the edit is confirmed as '{stale['invoice']} for {stale['due_date']} is already invoiced.' "
        f"instead of 'Schedule updated.'")


async def test_cadence_change_keeps_the_next_due_and_bills_monthly_from_there(api, other_merchant, db, restore_clock):
    """Edit weekly -> monthly with the dialog's "Next due" untouched: no extra invoice now, the next
    due keeps its date, and the one after it is a month later."""
    m = other_merchant
    t = today(db)
    await mark(api, m, email="asha@example.com")
    s = schedule_of(db, m["payer_a"])
    assert s["next_due_date"] == t + timedelta(days=7)
    out = await mark(api, m, cadence="monthly", email="asha@example.com", anchor_date=s["next_due_date"].isoformat())
    assert out["dues_created"] == []
    assert len(invoices_of(db, m["payer_a"])) == 1
    await jump(api, m["token"], days=5)  # the 7-day due enters the 3-day lead window
    invs = invoices_of(db, m["payer_a"])
    assert [i["due_date"] for i in invs] == [t, t + timedelta(days=7)]
    assert invs[1]["line_items"][0]["name"].startswith("Monthly payment")
    assert schedule_of(db, m["payer_a"])["next_due_date"] == next_due(t + timedelta(days=7), "monthly",
                                                                      t + timedelta(days=7))


async def test_editing_to_another_customers_email_is_refused_without_changes(api, other_merchant, db):
    m = other_merchant
    # In the app a customer's email on file is always one of their identifiers (the fixture skips that).
    db.execute("insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s, %s, 'email', "
               "'asha@example.com') on conflict do nothing", (m["payer_a"], m["merchant_id"]))
    await mark(api, m, payer_key="payer_b", email="bina@example.com",
               anchor_date=(today(db) + timedelta(days=10)).isoformat())
    before = schedule_of(db, m["payer_b"])
    r = await api.post(f"/payers/{m['payer_b']}/recurring", headers=bearer(m["token"]),
                       json={"cadence": "weekly", "amount_paise": 800_00, "email": "asha@example.com",
                             "anchor_date": before["next_due_date"].isoformat()})
    assert r.status_code == 409 and "Asha Test" in r.json()["detail"]
    after = schedule_of(db, m["payer_b"])
    assert (after["email"], after["amount_paise"]) == (before["email"], before["amount_paise"])


async def test_a_refused_mark_recurring_does_not_change_the_customer(api, other_merchant, db):
    """A request the API refuses (first due in the past, 422) must not leave half its effects behind:
    the customer's email and identifiers stay as they were."""
    m = other_merchant
    r = await api.post(f"/payers/{m['payer_b']}/recurring", headers=bearer(m["token"]),
                       json={"cadence": "weekly", "amount_paise": 800_00, "email": "bina.new@example.com",
                             "anchor_date": (today(db) - timedelta(days=1)).isoformat()})
    assert r.status_code == 422
    payer = db.execute("select email from payers where id = %s", (m["payer_b"],)).fetchone()
    idents = db.execute("select value from payer_identifiers where payer_id = %s and kind = 'email'",
                        (m["payer_b"],)).fetchall()
    assert (payer["email"], idents) == (None, []), (
        f"the refused request still saved the email: payers.email={payer['email']!r}, identifiers={idents}")


# ---------------------------------------------------------------------------
# Storage failures while a due is created (Collections flow step 3)
# ---------------------------------------------------------------------------
async def test_a_qr_storage_failure_while_marking_is_reported_like_a_paytm_failure(api, other_merchant, db, settings,
                                                                                   monkeypatch, restore_clock):
    """A Paytm failure while marking answers 201 with due_error ("Schedule saved, but the first invoice
    couldn't be created (...). It will be tried again automatically."). The QR upload to Storage is the
    same step of the flow; when it fails the merchant must get the same honest answer, not a 500 for a
    schedule that was in fact saved."""
    m = other_merchant

    async def storage_down(path, b64):
        raise httpx.ConnectError("storage unreachable")

    monkeypatch.setattr(inv_svc, "upload_qr_png", storage_down)
    r = await api.post(f"/payers/{m['payer_a']}/recurring", headers=bearer(m["token"]),
                       json={"cadence": "weekly", "amount_paise": 800_00, "grace_days": 0, "email": "asha@example.com"})
    saved = schedule_of(db, m["payer_a"])
    assert saved is not None  # the schedule exists either way
    assert r.status_code == 201 and r.json().get("due_error"), (
        f"POST answered {r.status_code} {r.text[:120]} although the schedule was saved")

    monkeypatch.undo()
    t = await api.post("/jobs/tick", headers=cron(settings))
    assert t.status_code == 200
    assert len(invoices_of(db, m["payer_a"])) == 1


async def test_a_storage_failure_for_one_due_does_not_abort_the_tick(api, other_merchant, db, monkeypatch,
                                                                     restore_clock):
    """The tick turns every schedule's upcoming due into an invoice. One customer's QR upload failing
    must not stop the other customers' dues (that is what the Paytm-failure path already does)."""
    m = other_merchant
    later = (today(db) + timedelta(days=5)).isoformat()
    await mark(api, m, payer_key="payer_a", email="asha@example.com", anchor_date=later)
    await mark(api, m, payer_key="payer_b", email="bina@example.com", anchor_date=later)
    assert invoices_of(db, m["payer_a"]) == [] and invoices_of(db, m["payer_b"]) == []

    original = inv_svc.attach_payment_artifacts

    async def flaky(conn, inv, merchant_name, payer):
        if payer["id"] == m["payer_a"]:
            raise httpx.ConnectError("storage unreachable")
        return await original(conn, inv, merchant_name, payer)

    monkeypatch.setattr(inv_svc, "attach_payment_artifacts", flaky)
    out = await jump(api, m["token"], days=3)
    monkeypatch.undo()
    dues = out["jobs"]["dues_created"]
    assert isinstance(dues, list), f"the whole dues step failed: {dues}"
    assert len(invoices_of(db, m["payer_b"])) == 1


# ---------------------------------------------------------------------------
# Monthly dues on the 29th-31st (pure date maths)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("anchor, expected", [
    (date(2027, 1, 29), [date(2027, 2, 28), date(2027, 3, 29), date(2027, 4, 29)]),
    (date(2028, 1, 31), [date(2028, 2, 29), date(2028, 3, 31), date(2028, 4, 30), date(2028, 5, 31)]),
    (date(2026, 10, 30), [date(2026, 11, 30), date(2026, 12, 30), date(2027, 1, 30), date(2027, 2, 28)]),
    (date(2026, 12, 31), [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)]),
])
def test_monthly_dues_stay_on_the_anchor_day_or_the_month_end(anchor, expected):
    got, d = [], anchor
    for _ in expected:
        d = next_due(d, "monthly", anchor)
        got.append(d)
    assert got == expected
