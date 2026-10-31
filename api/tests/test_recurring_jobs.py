"""M5: recurring schedules, /jobs/tick, demo clock, AI descriptions."""

import json
from datetime import date, timedelta

import psycopg
import pytest

from app.services import email as email_svc

from .conftest import bearer


@pytest.fixture(scope="module", autouse=True)
def reseed_after_module(reseed_demo):
    """Ticks run for every merchant (the demo one too); restore the seeded story afterwards."""
    yield
    reseed_demo()


@pytest.fixture(autouse=True)
def no_real_descriptions(monkeypatch):
    """Ticks describe new payments with Haiku; tests use a fixed answer instead."""
    async def fake_generate(t):
        return {"description": "Monthly dues payment", "category": "Services"}
    monkeypatch.setattr("app.services.descriptions.generate", fake_generate)


async def jump(api, token, days):
    r = await api.post("/admin/clock", json={"days": days}, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


async def test_recurring_with_email_creates_and_emails_first_due(api, other_merchant, restore_clock):
    email_svc.SENT.clear()
    h = bearer(other_merchant["token"])
    r = await api.post(f"/payers/{other_merchant['payer_a']}/recurring",
                       json={"cadence": "monthly", "amount_paise": 3000_00, "email": "asha@example.com"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["schedule"]["cadence"] == "monthly" and body["schedule"]["auto_remind"] is True
    assert not body["schedule"]["tracking_only"]
    assert len(body["dues_created"]) == 1 and body["dues_created"][0]["emailed"]
    assert "Recurring" in body["payer"]["badges"]
    assert email_svc.SENT and email_svc.SENT[-1]["intended_to"] == "asha@example.com"
    invs = (await api.get("/invoices", headers=h)).json()["invoices"]
    assert len(invs) == 1 and invs[0]["recurring_id"] == body["schedule"]["id"] and invs[0]["grace_days"] == 3


async def test_missed_due_triggers_one_reminder_after_clock_jump(api, other_merchant, restore_clock):
    h = bearer(other_merchant["token"])
    await api.post(f"/payers/{other_merchant['payer_a']}/recurring",
                   json={"cadence": "monthly", "amount_paise": 500_00, "email": "asha@example.com"}, headers=h)
    email_svc.SENT.clear()
    out = await jump(api, other_merchant["token"], 4)  # due today + 3 grace days → overdue on day 4
    inv = (await api.get("/invoices", headers=h)).json()["invoices"][0]
    assert inv["status"] == "overdue"
    assert inv["number"] in out["jobs"]["overdue"]["reminded"]
    reminder = [m for m in email_svc.SENT if m["subject"].startswith("Reminder: ₹500 due to Test Store")]
    assert len(reminder) == 1
    again = await jump(api, other_merchant["token"], 1)
    assert inv["number"] not in again["jobs"]["overdue"]["reminded"]


async def test_tracking_only_schedule_flags_missed_without_email(api, other_merchant, restore_clock):
    h = bearer(other_merchant["token"])
    r = await api.post(f"/payers/{other_merchant['payer_b']}/recurring",
                       json={"cadence": "weekly", "amount_paise": 200_00}, headers=h)
    body = r.json()
    assert body["schedule"]["tracking_only"] and body["schedule"]["auto_remind"] is False
    assert body["dues_created"] and not body["dues_created"][0]["emailed"]
    out = await jump(api, other_merchant["token"], 4)
    invs = (await api.get("/invoices", headers=h)).json()["invoices"]
    first = min(invs, key=lambda i: i["due_date"])
    assert first["status"] == "overdue" and first["number"] not in out["jobs"]["overdue"]["reminded"]
    assert first["reminder_count"] == 0


async def test_weekly_dues_catch_up_after_a_long_jump(api, other_merchant, restore_clock, db):
    h = bearer(other_merchant["token"])
    await api.post(f"/payers/{other_merchant['payer_b']}/recurring",
                   json={"cadence": "weekly", "amount_paise": 200_00}, headers=h)
    await jump(api, other_merchant["token"], 21)
    rows = db.execute("select due_date from invoices where merchant_id = %s order by due_date",
                      (other_merchant["merchant_id"],)).fetchall()
    gaps = {(b["due_date"] - a["due_date"]).days for a, b in zip(rows, rows[1:])}
    assert len(rows) >= 4 and gaps == {7}
    nxt = db.execute("select next_due_date from recurring_schedules where payer_id = %s and active",
                     (other_merchant["payer_b"],)).fetchone()["next_due_date"]
    assert nxt > rows[-1]["due_date"]


async def test_stop_recurring(api, other_merchant):
    h = bearer(other_merchant["token"])
    await api.post(f"/payers/{other_merchant['payer_a']}/recurring",
                   json={"cadence": "monthly", "amount_paise": 100_00}, headers=h)
    assert (await api.delete(f"/payers/{other_merchant['payer_a']}/recurring", headers=h)).status_code == 200
    assert (await api.delete(f"/payers/{other_merchant['payer_a']}/recurring", headers=h)).status_code == 404
    p = (await api.get(f"/payers/{other_merchant['payer_a']}", headers=h)).json()
    assert p["recurring_schedule"] is None and "Recurring" not in p["badges"]


@pytest.mark.parametrize("body,status", [
    ({"cadence": "daily", "amount_paise": 100_00}, 422),
    ({"cadence": "weekly", "amount_paise": 50}, 422),
    ({"cadence": "weekly", "amount_paise": 100_00, "anchor_date": (date.today() - timedelta(days=2)).isoformat()}, 422),
    ({"cadence": "weekly", "amount_paise": 100_00, "email": "not-an-email"}, 422),
])
async def test_recurring_validation(api, other_merchant, body, status):
    r = await api.post(f"/payers/{other_merchant['payer_a']}/recurring", json=body,
                       headers=bearer(other_merchant["token"]))
    assert r.status_code == status, r.text


async def test_jobs_tick_needs_cron_secret(api, settings):
    assert (await api.post("/jobs/tick")).status_code == 401
    assert (await api.post("/jobs/tick", headers={"X-Cron-Secret": "wrong"})).status_code == 401
    r = await api.post("/jobs/tick", headers={"X-Cron-Secret": settings.cron_secret})
    assert r.status_code == 200
    assert {"dues_created", "overdue", "paid_by_status_poll", "descriptions_written", "alerts"} <= set(r.json())


async def test_tick_is_single_flight(api, settings):
    with psycopg.connect(settings.database_url, autocommit=True) as other:
        other.execute("select pg_advisory_lock(4242001)")
        try:
            r = await api.post("/jobs/tick", headers={"X-Cron-Secret": settings.cron_secret})
            assert r.json() == {"skipped": "another tick is running"}
        finally:
            other.execute("select pg_advisory_unlock(4242001)")


async def test_clock_requires_auth_and_resets(api, demo_token, restore_clock):
    assert (await api.post("/admin/clock", json={"days": 1})).status_code == 401
    before = (await api.get("/admin/clock", headers=bearer(demo_token))).json()
    moved = (await api.post("/admin/clock", json={"days": 2, "run_jobs": False}, headers=bearer(demo_token))).json()
    assert moved["overridden"] and "jobs" not in moved
    from datetime import datetime

    delta = datetime.fromisoformat(moved["now"]) - datetime.fromisoformat(before["now"])
    assert timedelta(days=1, hours=23) < delta < timedelta(days=2, hours=1)
    me = (await api.get("/me", headers=bearer(demo_token))).json()
    assert me["clock_overridden"]
    reset = (await api.post("/admin/clock", json={"reset": True, "run_jobs": False}, headers=bearer(demo_token))).json()
    assert not reset["overridden"]
    assert (await api.post("/admin/clock", json={}, headers=bearer(demo_token))).status_code == 422


async def test_ai_descriptions_fill_blanks_but_not_merchant_edits(api, other_merchant, db, settings):
    m = other_merchant["merchant_id"]
    t1 = db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source) "
                    "values (%s, %s, 3000000, app_now(), 'mock') returning id", (m, other_merchant["payer_a"])).fetchone()["id"]
    t2 = db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source, description, "
                    "description_by) values (%s, %s, 100, app_now(), 'mock', 'Mine', 'merchant') returning id",
                    (m, other_merchant["payer_a"])).fetchone()["id"]
    r = await api.post("/jobs/tick", headers={"X-Cron-Secret": settings.cron_secret})
    assert r.json()["descriptions_written"] >= 1
    a = db.execute("select description, description_by, category from transactions where id = %s", (t1,)).fetchone()
    assert a == {"description": "Monthly dues payment", "description_by": "ai", "category": "Services"}
    b = db.execute("select description, description_by from transactions where id = %s", (t2,)).fetchone()
    assert b == {"description": "Mine", "description_by": "merchant"}


def test_next_due_keeps_the_anchor_day():
    from app.services.recurring import next_due

    anchor = date(2027, 1, 31)
    assert next_due(anchor, "monthly", anchor) == date(2027, 2, 28)
    assert next_due(date(2027, 2, 28), "monthly", anchor) == date(2027, 3, 31)
    assert next_due(date(2027, 12, 31), "monthly", anchor) == date(2028, 1, 31)
    assert next_due(date(2027, 1, 1), "weekly", anchor) == date(2027, 1, 8)


@pytest.mark.live
async def test_live_haiku_description(other_merchant, db):
    """Real Anthropic call (run with -m live)."""
    from app.db import connection
    from app.services.descriptions import _context, generate

    t = db.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source) "
                   "values (%s, %s, 12000, app_now(), 'mock') returning id",
                   (other_merchant["merchant_id"], other_merchant["payer_a"])).fetchone()["id"]
    async with connection() as conn:
        out = await generate(await _context(conn, t))
    assert out and 1 <= len(out["description"].split()) <= 8
    print(json.dumps(out))
