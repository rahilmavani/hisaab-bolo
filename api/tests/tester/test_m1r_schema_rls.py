"""Tester M1 (re-test): view semantics in IST, demo clock, and RLS through REST embedding.

Spec: ARCHITECTURE "Data model": times are timestamptz displayed in IST; payer_stats has count,
total, median amount, typical hour, last_paid_at and median gap in days; app_clock drives now();
every table has merchant_id and RLS is on. Mutating checks run in a rolled-back transaction.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from app.money import IST


@pytest.fixture
def tx(settings):
    with psycopg.connect(settings.database_url, autocommit=False, row_factory=dict_row) as conn:
        try:
            yield conn
        finally:
            conn.rollback()


def _merchant_with_payers(tx, n: int = 2):
    m = tx.execute("insert into merchants (name) values ('Tester Store') returning id").fetchone()["id"]
    ps = [tx.execute("insert into payers (merchant_id, display_name) values (%s, %s) returning id",
                     (m, f"P{i}")).fetchone()["id"] for i in range(n)]
    return m, ps


def _pay(tx, m, p, when: datetime, paise: int):
    tx.execute("insert into transactions (merchant_id, payer_id, amount_paise, paid_at, source) "
               "values (%s, %s, %s, %s, 'seed')", (m, p, paise, when))


# ---------------------------------------------------------------------------
# Views compute in IST
# ---------------------------------------------------------------------------
def test_payer_stats_values(tx):
    m, (p, solo) = _merchant_with_payers(tx)
    _pay(tx, m, p, datetime(2026, 10, 1, 8, 20, tzinfo=IST), 100_00)
    _pay(tx, m, p, datetime(2026, 10, 2, 8, 40, tzinfo=IST), 100_00)
    _pay(tx, m, p, datetime(2026, 10, 4, 8, 30, tzinfo=IST), 300_00)
    _pay(tx, m, solo, datetime(2026, 10, 4, 23, 50, tzinfo=IST), 50_00)
    s = tx.execute("select * from payer_stats where payer_id = %s", (p,)).fetchone()
    assert (s["txn_count"], s["total_paise"], s["median_amount_paise"]) == (3, 500_00, 100_00)
    assert s["typical_hour"] == 8, "typical hour must be the IST hour"
    assert s["last_paid_at"] == datetime(2026, 10, 4, 8, 30, tzinfo=IST)
    # gaps: 1d 20m and 1d 23h 50m -> median 1.50 days
    assert float(s["median_gap_days"]) == pytest.approx(1.50, abs=0.01)
    one = tx.execute("select * from payer_stats where payer_id = %s", (solo,)).fetchone()
    assert one["txn_count"] == 1 and one["median_gap_days"] is None and one["typical_hour"] == 23


def test_daily_totals_use_the_ist_calendar_day(tx):
    m, (p, q) = _merchant_with_payers(tx)
    # 00:30 IST on 5 Oct is still 4 Oct in UTC; it belongs to 5 Oct in the ledger.
    _pay(tx, m, p, datetime(2026, 10, 5, 0, 30, tzinfo=IST), 120_00)
    _pay(tx, m, q, datetime(2026, 10, 4, 23, 59, tzinfo=IST), 80_00)
    tx.execute("insert into transactions (merchant_id, amount_paise, paid_at, source) values (%s, %s, %s, 'seed')",
               (m, 40_00, datetime(2026, 10, 4, 12, 0, tzinfo=IST)))
    rows = {r["day"]: r for r in tx.execute(
        "select day, total_paise, txn_count, payer_count from daily_totals where merchant_id = %s", (m,))}
    assert set(rows) == {date(2026, 10, 4), date(2026, 10, 5)}
    assert (rows[date(2026, 10, 4)]["total_paise"], rows[date(2026, 10, 4)]["txn_count"],
            rows[date(2026, 10, 4)]["payer_count"]) == (120_00, 2, 1), "walk-ins count as payments, not payers"
    assert rows[date(2026, 10, 5)]["total_paise"] == 120_00


def test_heatmap_uses_ist_hour_and_isodow_and_the_demo_clock(tx):
    m, (p, _) = _merchant_with_payers(tx)
    when = datetime(2026, 10, 26, 8, 30, tzinfo=IST)  # Monday 08:30 IST = 03:00 UTC
    _pay(tx, m, p, when, 100_00)
    tx.execute("update app_clock set override_now = %s, set_at = now() where id = 1", (when + timedelta(days=1),))
    cell = tx.execute("select dow, hour, total_paise from hourly_heatmap where merchant_id = %s", (m,)).fetchall()
    assert [(c["dow"], c["hour"], c["total_paise"]) for c in cell] == [(1, 8, 100_00)]
    tx.execute("update app_clock set override_now = %s, set_at = now() where id = 1", (when - timedelta(hours=1),))
    assert tx.execute("select count(*) as n from hourly_heatmap where merchant_id = %s", (m,)).fetchone()["n"] == 0, \
        "a payment after the demo clock's now must not be in the heatmap"


def test_clock_can_jump_backwards_and_app_today_follows(tx):
    tx.execute("update app_clock set override_now = '2026-10-01 18:29:00+00', set_at = now() where id = 1")
    r = tx.execute("select app_now() as n, app_today() as d").fetchone()
    assert r["d"] == date(2026, 10, 1) and r["n"].year == 2026 and r["n"].month == 10
    tx.execute("update app_clock set override_now = '2026-10-01 18:31:00+00', set_at = now() where id = 1")
    assert tx.execute("select app_today() as d").fetchone()["d"] == date(2026, 10, 2)


# ---------------------------------------------------------------------------
# RLS through REST: embedded resources must not leak another merchant's rows
# ---------------------------------------------------------------------------
def _rest(settings, path, token):
    return httpx.get(f"{settings.supabase_url}/rest/v1/{path}", timeout=20,
                     headers={"apikey": settings.supabase_publishable_key, "Authorization": f"Bearer {token}"})


EMBED_PATHS = [
    "payers?select=display_name,transactions(amount_paise)",
    "payers?select=display_name,payer_identifiers(kind,value)",
    "invoices?select=number,payers(display_name),reminders(kind)",
    "transactions?select=amount_paise,invoices(number),payers(display_name)",
]


@pytest.mark.parametrize("path", EMBED_PATHS)
def test_embedded_reads_stay_inside_own_merchant(settings, demo_token, other_merchant, path):
    om_rows = _rest(settings, path, other_merchant["token"])
    assert om_rows.status_code == 200, om_rows.text[:200]
    text = om_rows.text
    for leaked in ("Ram", "Sharma Traders", "INV-0412", "ram@paytm", "Meena Kapoor"):
        assert leaked not in text, f"{path} leaks demo data ({leaked}) to another merchant"
    demo = _rest(settings, path, demo_token)
    assert demo.status_code == 200 and demo.json(), demo.text[:200]
    for leaked in ("Asha Test", "Asha T (UPI)", "asha@okaxis", "asha.t@ybl"):
        assert leaked not in demo.text, f"{path} leaks the other merchant ({leaked}) to the demo owner"


def test_other_merchant_counts_only_its_rows(settings, other_merchant):
    r = httpx.get(f"{settings.supabase_url}/rest/v1/transactions?select=id", timeout=20,
                  headers={"apikey": settings.supabase_publishable_key,
                           "Authorization": f"Bearer {other_merchant['token']}", "Prefer": "count=exact"})
    assert r.status_code in (200, 206)
    assert r.headers["content-range"].endswith("/3"), r.headers["content-range"]


def test_sql_level_rls_for_views_by_jwt_subject(tx, settings, seeded):
    """Same check below PostgREST: the authenticated role only sees its own merchant in the views."""
    owner = tx.execute("select id from auth.users where email = %s", (settings.demo_merchant_email,)).fetchone()["id"]
    tx.execute("set local role authenticated")
    for sub, expect_rows in ((str(owner), True), (str(uuid.uuid4()), False)):
        tx.execute("select set_config('request.jwt.claims', %s, true)", (f'{{"sub": "{sub}", "role": "authenticated"}}',))
        for view in ("payer_stats", "daily_totals", "hourly_heatmap", "payers", "transactions", "invoices"):
            n = tx.execute(f"select count(*) as n from public.{view}").fetchone()["n"]
            assert (n > 0) == expect_rows, f"{view}: {n} rows for sub={'owner' if expect_rows else 'stranger'}"
