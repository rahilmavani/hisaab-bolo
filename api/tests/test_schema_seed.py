"""M1: schema, demo clock and seeded demo story."""

from datetime import timedelta

import httpx
import pytest

TABLES = [
    "merchants", "payers", "payer_identifiers", "transactions", "invoices",
    "recurring_schedules", "reminders", "webhook_events", "agent_queries",
    "pending_actions", "app_clock", "paytm_mock_orders",
]


def test_all_tables_exist_with_rls(db):
    rows = db.execute(
        "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r'"
    ).fetchall()
    rls = {r["relname"]: r["relrowsecurity"] for r in rows}
    for t in TABLES:
        assert t in rls, f"missing table {t}"
        assert rls[t], f"RLS is off on {t}"


def test_views_exist(db):
    views = {r["viewname"] for r in db.execute("select viewname from pg_views where schemaname = 'public'")}
    assert {"daily_totals", "hourly_heatmap", "payer_stats"} <= views


def test_money_columns_are_integer_paise(db):
    rows = db.execute(
        "select table_name, column_name, data_type from information_schema.columns "
        "where table_schema = 'public' and column_name like '%%paise'"
    ).fetchall()
    assert rows
    for r in rows:
        if r["table_name"] in ("daily_totals", "hourly_heatmap", "payer_stats"):
            continue
        assert r["data_type"] in ("bigint", "integer"), r


def test_app_clock_jump_and_reset(db, restore_clock):
    real = db.execute("select now() as now, app_now() as app").fetchone()
    assert abs((real["app"] - real["now"]).total_seconds()) < 1
    db.execute(
        "update app_clock set override_now = now() + interval '8 days', set_at = now() where id = 1"
    )
    jumped = db.execute("select now() as now, app_now() as app, app_today() as today").fetchone()
    assert timedelta(days=8) - timedelta(seconds=1) < jumped["app"] - jumped["now"] < timedelta(days=8, seconds=1)
    db.execute("update app_clock set override_now = null, set_at = null where id = 1")
    back = db.execute("select now() as now, app_now() as app").fetchone()
    assert abs((back["app"] - back["now"]).total_seconds()) < 1


def test_next_invoice_number_increments(db, seeded):
    m = seeded["merchant_id"]
    seq = db.execute("select invoice_seq from merchants where id = %s", (m,)).fetchone()["invoice_seq"]
    a = db.execute("select next_invoice_number(%s) as n", (m,)).fetchone()["n"]
    b = db.execute("select next_invoice_number(%s) as n", (m,)).fetchone()["n"]
    assert a == f"INV-{seq + 1:04d}" and b == f"INV-{seq + 2:04d}"
    db.execute("update merchants set invoice_seq = %s where id = %s", (seq, m))


def test_seed_volume(db, seeded):
    m = seeded["merchant_id"]
    payers = db.execute("select count(*) as n from payers where merchant_id = %s", (m,)).fetchone()["n"]
    assert 35 <= payers <= 45
    stats = db.execute(
        "select count(*) as n, min(paid_at) as first, max(paid_at) as last, bool_and(amount_paise > 0) as pos "
        "from transactions where merchant_id = %s", (m,)
    ).fetchone()
    assert 7000 <= stats["n"] <= 9000
    assert stats["pos"]
    now = db.execute("select app_now() as now").fetchone()["now"]
    assert stats["last"] <= now, "no future-dated payments"
    walk_in_days = db.execute(
        "select count(distinct (paid_at at time zone 'Asia/Kolkata')::date) as d from transactions "
        "where merchant_id = %s and payer_id is null", (m,)
    ).fetchone()["d"]
    assert walk_in_days >= 90


def test_ram_story(db, seeded):
    m = seeded["merchant_id"]
    row = db.execute(
        "select s.*, p.id as pid from payer_stats s join payers p on p.id = s.payer_id "
        "where p.merchant_id = %s and p.display_name = 'Ram'", (m,)
    ).fetchone()
    assert row["txn_count"] == 121
    assert row["total_paise"] == 121 * 100_00
    assert row["median_amount_paise"] == 100_00
    assert 8 * 60 + 15 <= row["typical_minute"] <= 8 * 60 + 45
    assert float(row["median_gap_days"]) == pytest.approx(1.0, abs=0.05)
    window = db.execute(
        "select count(*) as n, sum(amount_paise) as total from transactions where payer_id = %s "
        "and paid_at >= ((app_today() - interval '4 months')::date)::timestamp at time zone 'Asia/Kolkata'",
        (row["pid"],),
    ).fetchone()
    assert window["n"] == 121 and window["total"] == 12_100_00
    vpa = db.execute(
        "select value from payer_identifiers where payer_id = %s and kind = 'vpa'", (row["pid"],)
    ).fetchone()
    assert vpa["value"] == "ram@paytm"


def test_collections_story(db, seeded):
    m = seeded["merchant_id"]
    inv = {
        r["number"]: r for r in db.execute(
            "select i.*, p.display_name, p.gstin, app_today() as today from invoices i "
            "join payers p on p.id = i.payer_id where i.merchant_id = %s", (m,)
        )
    }
    sharma = inv["INV-0412"]
    assert sharma["display_name"] == "Sharma Traders"
    assert sharma["amount_paise"] == 8500_00 and sharma["status"] == "overdue"
    assert (sharma["today"] - sharma["due_date"]).days == 9
    assert sharma["gst_taxable"] and sharma["payer_gstin"] == sharma["gstin"]
    open_rows = [r for r in inv.values() if r["status"] in ("sent", "overdue")]
    assert sum(r["amount_paise"] for r in open_rows) == 11_700_00
    assert {r["display_name"] for r in open_rows} == {"Sharma Traders", "Priya Caterers"}
    priya = next(r for r in open_rows if r["display_name"] == "Priya Caterers")
    assert (priya["due_date"] - priya["today"]).days == 1
    paid = [r for r in inv.values() if r["status"] == "paid"]
    for r in paid:
        t = db.execute("select amount_paise from transactions where invoice_id = %s", (r["id"],)).fetchall()
        assert len(t) == 1 and t[0]["amount_paise"] == r["amount_paise"], r["number"]
    assert any(r["display_name"] == "Anita Desai" and r["amount_paise"] == 1500_00 for r in paid)


def test_exactly_one_lost_regular(db, seeded):
    rows = db.execute(
        "select p.display_name, extract(epoch from app_now() - s.last_paid_at) / 86400 as days "
        "from payer_stats s join payers p on p.id = s.payer_id where p.merchant_id = %s "
        "and s.txn_count >= 10 and app_now() - s.last_paid_at > 3 * s.median_gap_days * interval '1 day'",
        (seeded["merchant_id"],),
    ).fetchall()
    assert [r["display_name"] for r in rows] == ["Meena Kapoor"]
    assert 11.5 <= float(rows[0]["days"]) <= 13


def test_rls_scopes_rest_reads_to_owner(settings, seeded, demo_token):
    base = f"{settings.supabase_url}/rest/v1"
    anon = httpx.get(f"{base}/payers?select=id", headers={"apikey": settings.supabase_publishable_key})
    assert anon.status_code in (200, 401) and (anon.status_code == 401 or anon.json() == [])
    owner = httpx.get(
        f"{base}/payers?select=id,merchant_id",
        headers={"apikey": settings.supabase_publishable_key, "Authorization": f"Bearer {demo_token}"},
    )
    assert owner.status_code == 200
    rows = owner.json()
    assert len(rows) == 40 and {r["merchant_id"] for r in rows} == {seeded["merchant_id"]}
    hidden = httpx.get(
        f"{base}/webhook_events?select=id",
        headers={"apikey": settings.supabase_publishable_key, "Authorization": f"Bearer {demo_token}"},
    )
    assert hidden.status_code in (401, 403) or hidden.json() == []


def test_qr_bucket_is_public(db):
    row = db.execute("select public from storage.buckets where id = 'qr'").fetchone()
    assert row and row["public"]


def test_realtime_publication(db):
    rows = db.execute(
        "select tablename from pg_publication_tables where pubname = 'supabase_realtime'"
    ).fetchall()
    assert {"invoices", "transactions"} <= {r["tablename"] for r in rows}
