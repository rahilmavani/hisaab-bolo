"""Tester M1: the demo story must hold whenever the seed is run (the team re-seeds on demo day).

Each instant re-seeds with `seed(now=...)` and points the demo clock at that instant so
app_now()/app_today() agree with the data. The module always restores a real-now seed and
clears the clock when it finishes.
"""

from __future__ import annotations

from datetime import datetime

import psycopg
import pytest

from app.money import IST
from app.seed import seed

from .test_m1_seed_data import slow_day_ratio

INSTANTS = {
    "Fri-30Oct-07:00": datetime(2026, 10, 30, 7, 0, tzinfo=IST),
    "Fri-30Oct-10:00": datetime(2026, 10, 30, 10, 0, tzinfo=IST),
    "Fri-30Oct-13:00": datetime(2026, 10, 30, 13, 0, tzinfo=IST),
    "Fri-30Oct-16:00": datetime(2026, 10, 30, 16, 0, tzinfo=IST),
    "Sun-01Nov-10:00": datetime(2026, 11, 1, 10, 0, tzinfo=IST),
    "Mon-02Nov-12:00": datetime(2026, 11, 2, 12, 0, tzinfo=IST),
    "Wed-11Nov-10:00": datetime(2026, 11, 11, 10, 0, tzinfo=IST),
    "Sat-14Nov-19:00": datetime(2026, 11, 14, 19, 0, tzinfo=IST),
}


@pytest.fixture(scope="module", autouse=True)
def _restore_real_seed(settings):
    yield
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("update app_clock set override_now = null, set_at = null where id = 1")
    seed(verbose=False)


@pytest.fixture(scope="module", params=list(INSTANTS.values()), ids=list(INSTANTS))
def seeded_at(request, settings):
    now = request.param
    summary = seed(now=now, verbose=False)
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("update app_clock set override_now = %s, set_at = now() where id = 1", (now,))
    return now, summary["merchant_id"]


def test_core_story_holds(db, seeded_at):
    now, mid = seeded_at
    today = now.date()
    ram = db.execute(
        "select count(*) as n, sum(t.amount_paise) as s from transactions t join payers p on p.id = t.payer_id "
        "where p.merchant_id = %s and p.display_name = 'Ram' and t.paid_at >= "
        "((app_today() - interval '4 months')::date)::timestamp at time zone 'Asia/Kolkata'", (mid,)).fetchone()
    assert (ram["n"], ram["s"]) == (121, 12_100_00)
    inv = db.execute("select status, due_date from invoices where merchant_id = %s and number = 'INV-0412'",
                     (mid,)).fetchone()
    assert inv["status"] == "overdue" and (today - inv["due_date"]).days == 9
    open_total = db.execute("select sum(amount_paise) as s from invoices where merchant_id = %s "
                            "and status in ('sent', 'overdue')", (mid,)).fetchone()["s"]
    assert open_total == 11_700_00
    future = db.execute("select count(*) as n from transactions where merchant_id = %s and paid_at > app_now()",
                        (mid,)).fetchone()["n"]
    assert future == 0


def test_exactly_one_lost_regular_stopped_12_days_ago(db, seeded_at):
    now, mid = seeded_at
    rows = db.execute(
        "select p.display_name, (s.last_paid_at at time zone 'Asia/Kolkata')::date as last_day "
        "from payer_stats s join payers p on p.id = s.payer_id where s.merchant_id = %s "
        "and s.txn_count >= 10 and app_now() - s.last_paid_at > 3 * s.median_gap_days * interval '1 day'",
        (mid,)).fetchall()
    assert len(rows) == 1, [r["display_name"] for r in rows]
    gap = (now.date() - rows[0]["last_day"]).days
    assert gap == 12, f"{rows[0]['display_name']} last paid {gap} days ago (spec: stopped 12 days ago)"


def test_every_seeded_payer_has_a_payment(db, seeded_at):
    _, mid = seeded_at
    rows = db.execute(
        "select display_name from payers p where merchant_id = %s "
        "and not exists (select 1 from transactions t where t.payer_id = p.id)", (mid,)).fetchall()
    assert not rows, f"payers with zero payments: {[r['display_name'] for r in rows]}"


def test_today_is_a_slow_day(db, seeded_at):
    """Slow-day banner: today up to this hour >= 30% below the same weekday's 4-week average."""
    _, mid = seeded_at
    ratio = slow_day_ratio(db, mid)
    assert ratio <= 0.70, f"today is {100 * (1 - ratio):.1f}% below the 4-week same-weekday average (needs >= 30%)"
