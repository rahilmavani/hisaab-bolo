"""Tester M1: re-running the seed wipes and recreates only the demo merchant."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.money import IST
from app.seed import seed

DEMO_TABLES = ("payers", "payer_identifiers", "transactions", "invoices", "reminders")


@pytest.fixture(scope="module", autouse=True)
def _leave_a_real_now_seed(settings):
    yield
    import psycopg

    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("update app_clock set override_now = null, set_at = null where id = 1")
    seed(verbose=False)


def test_reseed_keeps_one_user_and_one_merchant(db, settings):
    first = seed(verbose=False)
    second = seed(verbose=False)
    users = db.execute("select id from auth.users where email = %s", (settings.demo_merchant_email,)).fetchall()
    assert len(users) == 1
    merchants = db.execute("select id, name from merchants where owner_user_id = %s", (users[0]["id"],)).fetchall()
    assert [str(m["id"]) for m in merchants] == [second["merchant_id"]]
    gupta = db.execute("select count(*) as n from merchants where name = 'Gupta Kirana Store'").fetchone()["n"]
    assert gupta == 1, f"{gupta} 'Gupta Kirana Store' merchants after two seeds"
    assert db.execute("select count(*) as n from merchants where id = %s", (first["merchant_id"],)).fetchone()["n"] == 0
    for t in DEMO_TABLES:
        n = db.execute(f"select count(*) as n from {t} where merchant_id = %s", (first["merchant_id"],)).fetchone()["n"]
        assert n == 0, f"{n} orphaned {t} rows from the previous seed"
    assert second["payers"] == first["payers"]


def test_reseed_resets_a_jumped_clock(db):
    db.execute("update app_clock set override_now = now() + interval '9 days', set_at = now() where id = 1")
    seed(verbose=False)
    from app.seed import demo_instant

    row = db.execute("select override_now, now() as now from app_clock where id = 1").fetchone()
    expected, pinned = demo_instant(row["now"])
    assert row["override_now"] == (expected if pinned else None)  # the 9-day jump is gone


def test_seed_is_deterministic_for_a_given_instant(db, settings):
    at = datetime(2026, 10, 30, 21, 0, tzinfo=IST)

    def snapshot():
        seed(now=at, verbose=False)
        mid = db.execute(
            "select m.id from merchants m join auth.users u on u.id = m.owner_user_id where u.email = %s",
            (settings.demo_merchant_email,)).fetchone()["id"]
        return (
            sorted(r["display_name"] for r in db.execute("select display_name from payers where merchant_id = %s", (mid,))),
            db.execute("select count(*) as n, sum(amount_paise) as s from transactions where merchant_id = %s",
                       (mid,)).fetchone(),
            sorted((r["number"], r["amount_paise"], r["status"]) for r in db.execute(
                "select number, amount_paise, status from invoices where merchant_id = %s", (mid,))),
        )

    assert snapshot() == snapshot()


def test_reseed_leaves_other_merchants_alone(db, other_merchant):
    om = other_merchant["merchant_id"]
    seed(verbose=False)
    assert db.execute("select count(*) as n from payers where merchant_id = %s", (om,)).fetchone()["n"] == 2
    assert db.execute("select count(*) as n from transactions where merchant_id = %s", (om,)).fetchone()["n"] == 3
