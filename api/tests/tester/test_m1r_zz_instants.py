"""Tester M1 (re-test): the seeded story must hold whenever the seed is run.

The team re-seeds on demo day at an unknown time, so each instant below re-seeds with
`seed(now=...)` and points the demo clock (app_clock) at the same instant, so app_now() /
app_today() agree with the data. Instants cover every weekday, early/late hours and the
boundaries the seed special-cases (08:45 for Ram, 10:00 for newcomers, midnight IST).

The module always clears the clock and leaves a fresh real-time seed when it finishes.
"""

from __future__ import annotations

from datetime import datetime, time

import psycopg
import pytest

from app.money import IST
from app.seed import seed

from ..conftest import bearer


def _ist(*a) -> datetime:
    return datetime(*a, tzinfo=IST)


INSTANTS = {
    # Spread over weekdays and times of day.
    "Fri-30Oct-00:45": _ist(2026, 10, 30, 0, 45),
    "Sat-31Oct-07:30": _ist(2026, 10, 31, 7, 30),
    "Sun-01Nov-12:00": _ist(2026, 11, 1, 12, 0),
    "Tue-03Nov-18:45": _ist(2026, 11, 3, 18, 45),
    "Thu-05Nov-09:30": _ist(2026, 11, 5, 9, 30),
    "Fri-13Nov-21:30": _ist(2026, 11, 13, 21, 30),
    "Wed-18Nov-23:45": _ist(2026, 11, 18, 23, 45),
    # Instants found by sweeping seed(now=...) over the coming weeks.
    "Fri-30Oct-08:30": _ist(2026, 10, 30, 8, 30),
    "Mon-02Nov-06:30": _ist(2026, 11, 2, 6, 30),
    "Mon-23Nov-20:10": _ist(2026, 11, 23, 20, 10),
    "Sat-28Nov-14:00": _ist(2026, 11, 28, 14, 0),
    "Wed-02Dec-11:00": _ist(2026, 12, 2, 11, 0),
    "Sat-05Dec-10:00": _ist(2026, 12, 5, 10, 0),
    "Mon-15Mar2027-12:00": _ist(2027, 3, 15, 12, 0),
}


@pytest.fixture(scope="module", autouse=True)
def _leave_fresh_real_seed(settings):
    yield
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("update app_clock set override_now = null, set_at = null where id = 1")
    seed(verbose=False)


@pytest.fixture(scope="module", params=list(INSTANTS.values()), ids=list(INSTANTS))
def at(request, settings):
    now = request.param
    summary = seed(now=now, verbose=False)
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("update app_clock set override_now = %s, set_at = now() where id = 1", (now,))
    return now, summary["merchant_id"]


def test_volume(db, at):
    _, mid = at
    payers = db.execute("select count(*) as n from payers where merchant_id = %s", (mid,)).fetchone()["n"]
    txns = db.execute("select count(*) as n from transactions where merchant_id = %s", (mid,)).fetchone()["n"]
    assert 35 <= payers <= 45 and 7000 <= txns <= 9000, (payers, txns)


def test_every_payer_paid_and_nothing_in_the_future(db, at):
    _, mid = at
    unpaid = db.execute(
        "select display_name from payers p where merchant_id = %s "
        "and not exists (select 1 from transactions t where t.payer_id = p.id)", (mid,)).fetchall()
    assert not unpaid, [r["display_name"] for r in unpaid]
    future = db.execute(
        "select (select count(*) from transactions where merchant_id = %(m)s and paid_at > app_now()) "
        "     + (select count(*) from invoices where merchant_id = %(m)s and "
        "        greatest(created_at, sent_at, paid_at) > app_now()) as n", {"m": mid}).fetchone()["n"]
    assert future == 0


def test_ram_121_payments_of_100_in_last_4_months(db, at):
    """Deck slides 4-5: Rs 100 daily around 8:30; Rs 12,100 / 121 payments in the last 4 months."""
    now, mid = at
    r = db.execute(
        "select count(*) filter (where t.paid_at >= ((app_today() - interval '4 months')::date)::timestamp "
        "         at time zone 'Asia/Kolkata') as n4, "
        "       sum(t.amount_paise) filter (where t.paid_at >= ((app_today() - interval '4 months')::date)::timestamp "
        "         at time zone 'Asia/Kolkata') as s4, "
        "       count(*) as n_all, max(t.paid_at) as last "
        "from transactions t join payers p on p.id = t.payer_id where p.merchant_id = %s and p.display_name = 'Ram'",
        (mid,)).fetchone()
    assert (r["n4"], r["s4"], r["n_all"]) == (121, 12_100_00, 121), (r["n4"], r["s4"], r["n_all"])
    if now.time() >= time(9, 0):
        assert r["last"].astimezone(IST).date() == now.date(), "deck: 'Last payment: today'"


def test_collections_story(db, at):
    """Deck slide 6: INV-0412 Rs 8,500 overdue 9 days; Priya Rs 3,200 due tomorrow; Rs 11,700 across 2."""
    now, mid = at
    rows = db.execute(
        "select i.number, i.amount_paise, i.status, i.due_date, p.display_name from invoices i "
        "join payers p on p.id = i.payer_id where i.merchant_id = %s and i.status in ('sent', 'overdue')",
        (mid,)).fetchall()
    by = {r["display_name"]: r for r in rows}
    assert set(by) == {"Sharma Traders", "Priya Caterers"} and len(rows) == 2
    s, p = by["Sharma Traders"], by["Priya Caterers"]
    assert (s["number"], s["amount_paise"], s["status"]) == ("INV-0412", 8_500_00, "overdue")
    assert (now.date() - s["due_date"]).days == 9
    assert (p["amount_paise"], p["status"], (p["due_date"] - now.date()).days) == (3_200_00, "sent", 1)
    assert sum(r["amount_paise"] for r in rows) == 11_700_00


def test_exactly_one_lost_regular_stopped_12_days_ago(db, at):
    """Lost-regular alert: now - last_paid_at > 3 x median gap and at least 10 payments.
    Spec: exactly one regular who stopped 12 days ago."""
    now, mid = at
    rows = db.execute(
        "select p.display_name, s.txn_count, s.median_gap_days, "
        "round((extract(epoch from app_now() - s.last_paid_at) / 86400)::numeric, 2) as days_since, "
        "(s.last_paid_at at time zone 'Asia/Kolkata')::date as last_day "
        "from payer_stats s join payers p on p.id = s.payer_id where s.merchant_id = %s "
        "and s.txn_count >= 10 and app_now() - s.last_paid_at > 3 * s.median_gap_days * interval '1 day'",
        (mid,)).fetchall()
    assert len(rows) == 1, "lost regulars: " + "; ".join(
        f"{r['display_name']} ({r['txn_count']} payments, median gap {r['median_gap_days']}d, "
        f"last paid {r['days_since']}d ago)" for r in rows)
    assert (now.date() - rows[0]["last_day"]).days == 12


def test_slow_day_condition(db, at):
    """Slow-day banner: today up to this hour >= 30% below the same weekday's 4-week average to the same hour."""
    _, mid = at
    rows = {r["k"]: r["paise"] for r in db.execute(
        "with n as (select app_now() as now, app_today() as today) "
        "select k, coalesce(sum(t.amount_paise), 0)::bigint as paise from generate_series(0, 4) k "
        "left join transactions t on t.merchant_id = %s "
        " and t.paid_at >= (((select today from n) - 7 * k)::timestamp at time zone 'Asia/Kolkata') "
        " and t.paid_at <= (select now from n) - (7 * k) * interval '1 day' group by k", (mid,))}
    baseline = sum(rows[k] for k in (1, 2, 3, 4)) / 4
    if baseline == 0:
        pytest.skip("shop not open yet at this instant: no baseline to compare against")
    ratio = rows[0] / baseline
    assert ratio <= 0.70, (f"today so far Rs {rows[0] / 100:.0f} vs 4-week average Rs {baseline / 100:.0f} "
                           f"({100 * (ratio - 1):+.0f}%), needs <= -30%")


async def test_dashboard_api_shows_the_story(api, demo_token, at):
    """What the dashboard renders: Gone quiet list, pending payments, slow-day banner (after 08:00)."""
    now, _ = at
    r = await api.get("/dashboard/summary", headers=bearer(demo_token))
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["outstanding"]["total_paise"] == 11_700_00 and d["outstanding"]["customers"] == 2
    assert [p["display_name"] for p in d["lost_regulars"]] == ["Meena Kapoor"], \
        f"Gone quiet shows {[p['display_name'] for p in d['lost_regulars']]}"
    if now.time() >= time(8, 0):
        assert d["slow_day"]["is_slow"], d["slow_day"]
