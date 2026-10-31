"""Tester M1: schema contract (docs/ARCHITECTURE.md "Data model" and PLAN.md M1 line).

Written by the independent tester. Every check that mutates state runs inside a
transaction that is rolled back, so the shared demo database is left untouched.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

# Columns the spec names for each table (extra columns are allowed).
SPEC_COLUMNS = {
    "merchants": {"id", "name", "paytm_mid", "owner_user_id"},
    "payers": {"id", "merchant_id", "display_name", "email", "phone", "gstin", "notes", "merged_into"},
    "payer_identifiers": {"payer_id", "merchant_id", "kind", "value"},
    "transactions": {
        "id", "merchant_id", "payer_id", "amount_paise", "paid_at", "source", "paytm_order_id",
        "paytm_txn_id", "payment_mode", "description", "description_by", "category", "gst_taxable",
        "invoice_id", "raw",
    },
    "invoices": {
        "id", "merchant_id", "number", "payer_id", "amount_paise", "line_items", "gst_taxable",
        "payer_gstin", "due_date", "status", "paytm_order_id", "qr_order_id", "link_id", "link_url",
        "qr_image_url", "recurring_id", "paytm_raw", "sent_at", "paid_at",
    },
    "recurring_schedules": {
        "id", "merchant_id", "payer_id", "amount_paise", "cadence", "anchor_date", "next_due_date",
        "grace_days", "auto_remind", "active",
    },
    "reminders": {"id", "invoice_id", "kind", "sent_at", "resend_id"},
    "webhook_events": {"id", "provider", "external_id", "payload", "processed_at", "error"},
    "agent_queries": {
        "id", "merchant_id", "transcript", "lang", "tool_calls", "answer", "latency_ms", "cost_paise",
    },
    "pending_actions": {"token", "merchant_id", "tool", "args", "expires_at", "confirmed_at"},
    "app_clock": {"id", "override_now"},
}

# Columns the spec marks optional with "?".
SPEC_NULLABLE = {
    ("payers", "email"), ("payers", "phone"), ("payers", "gstin"), ("payers", "merged_into"),
    ("transactions", "payer_id"), ("transactions", "invoice_id"),
    ("invoices", "payer_gstin"), ("invoices", "qr_order_id"), ("invoices", "recurring_id"),
    ("app_clock", "override_now"),
}

SPEC_JSONB = {
    ("transactions", "raw"), ("invoices", "line_items"), ("invoices", "paytm_raw"),
    ("webhook_events", "payload"), ("agent_queries", "tool_calls"), ("pending_actions", "args"),
}

# Enumerations written as name[a|b|c] in the spec.
SPEC_ENUMS = {
    ("transactions", "source"): {"seed", "paytm_link", "paytm_qr", "mock"},
    ("transactions", "description_by"): {"ai", "merchant"},
    ("invoices", "status"): {"draft", "sent", "overdue", "paid", "cancelled"},
    ("recurring_schedules", "cadence"): {"weekly", "monthly"},
    ("reminders", "kind"): {"invoice", "overdue"},
    ("payer_identifiers", "kind"): {"vpa", "phone", "email", "paytm_cust_id"},
}

VIEWS = ("daily_totals", "hourly_heatmap", "payer_stats")


def _columns(db, table: str) -> dict[str, dict]:
    rows = db.execute(
        "select column_name, data_type, is_nullable from information_schema.columns "
        "where table_schema = 'public' and table_name = %s", (table,),
    ).fetchall()
    return {r["column_name"]: r for r in rows}


@pytest.fixture
def tx(settings):
    """A non-autocommit connection whose work is always rolled back."""
    with psycopg.connect(settings.database_url, autocommit=False, row_factory=dict_row) as conn:
        try:
            yield conn
        finally:
            conn.rollback()


# ---------------------------------------------------------------------------
# Tables and columns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", sorted(SPEC_COLUMNS))
def test_table_has_spec_columns(db, table):
    cols = _columns(db, table)
    assert cols, f"table {table} is missing"
    missing = SPEC_COLUMNS[table] - set(cols)
    assert not missing, f"{table} lacks spec columns {sorted(missing)}"


def test_optional_columns_are_nullable(db):
    for table, col in SPEC_NULLABLE:
        assert _columns(db, table)[col]["is_nullable"] == "YES", f"{table}.{col} should be optional"


def test_business_tables_require_merchant_id(db):
    """'every table has merchant_id' (app_clock is the global demo clock)."""
    tables = [r["relname"] for r in db.execute(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r'"
    )]
    for t in tables:
        if t in ("app_clock", "merchants"):
            continue
        assert "merchant_id" in _columns(db, t), f"{t} has no merchant_id"
    for t in ("payers", "payer_identifiers", "transactions", "invoices", "recurring_schedules",
              "reminders", "agent_queries", "pending_actions"):
        assert _columns(db, t)["merchant_id"]["is_nullable"] == "NO", f"{t}.merchant_id must be NOT NULL"


def test_jsonb_columns(db):
    for table, col in SPEC_JSONB:
        assert _columns(db, table)[col]["data_type"] == "jsonb", f"{table}.{col} should be jsonb"


@pytest.mark.parametrize("table,col", sorted(SPEC_ENUMS))
def test_enum_check_matches_spec(db, table, col):
    defs = [r["d"] for r in db.execute(
        "select pg_get_constraintdef(c.oid) as d from pg_constraint c "
        "where c.conrelid = %s::regclass and c.contype = 'c'", (f"public.{table}",),
    )]
    matching = [d for d in defs if re.search(rf"\b{col}\b", d)]
    assert matching, f"no CHECK constraint on {table}.{col}"
    allowed = set(re.findall(r"'([^']+)'::text", matching[0]))
    assert allowed == SPEC_ENUMS[(table, col)], f"{table}.{col} allows {sorted(allowed)}"


def test_money_is_integer_paise_everywhere(db):
    """All money in integer paise: no numeric/float money columns anywhere in public."""
    rows = db.execute(
        "select table_name, column_name, data_type from information_schema.columns "
        "where table_schema = 'public' and (column_name like '%paise%' or column_name like '%amount%' "
        "or column_name like '%total%' or column_name like '%price%')"
    ).fetchall()
    assert rows
    bad = [(r["table_name"], r["column_name"], r["data_type"]) for r in rows
           if r["data_type"] not in ("bigint", "integer")]
    assert not bad, f"non-integer money columns: {bad}"


def test_all_times_are_timestamptz(db):
    rows = db.execute(
        "select table_name, column_name from information_schema.columns "
        "where table_schema = 'public' and data_type = 'timestamp without time zone'"
    ).fetchall()
    assert not rows, f"timestamp without time zone: {rows}"
    for table, col in [("transactions", "paid_at"), ("invoices", "sent_at"), ("invoices", "paid_at"),
                       ("reminders", "sent_at"), ("pending_actions", "expires_at"),
                       ("app_clock", "override_now"), ("webhook_events", "processed_at")]:
        assert _columns(db, table)[col]["data_type"] == "timestamp with time zone", f"{table}.{col}"


# ---------------------------------------------------------------------------
# Uniqueness and indexes
# ---------------------------------------------------------------------------
def test_payer_identifier_unique_per_merchant(tx):
    """unique (merchant_id, kind, value): duplicates rejected within a merchant, allowed across merchants."""
    m1 = tx.execute("insert into merchants (name) values ('T1') returning id").fetchone()["id"]
    m2 = tx.execute("insert into merchants (name) values ('T2') returning id").fetchone()["id"]
    p1 = tx.execute("insert into payers (merchant_id, display_name) values (%s, 'A') returning id", (m1,)).fetchone()["id"]
    p1b = tx.execute("insert into payers (merchant_id, display_name) values (%s, 'B') returning id", (m1,)).fetchone()["id"]
    p2 = tx.execute("insert into payers (merchant_id, display_name) values (%s, 'A') returning id", (m2,)).fetchone()["id"]
    ins = "insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s, %s, 'vpa', 'dup@ybl')"
    tx.execute(ins, (p1, m1))
    tx.execute(ins, (p2, m2))  # same VPA at another merchant is fine
    tx.execute("savepoint s")
    with pytest.raises(psycopg.errors.UniqueViolation):
        tx.execute(ins, (p1b, m1))
    tx.execute("rollback to savepoint s")


def test_webhook_events_unique_provider_external_id(tx):
    ins = "insert into webhook_events (provider, external_id, payload) values ('paytm', 'TXN1', '{}')"
    tx.execute(ins)
    with pytest.raises(psycopg.errors.UniqueViolation):
        tx.execute(ins)


def test_invoice_paytm_order_id_unique(tx):
    m = tx.execute("insert into merchants (name) values ('T') returning id").fetchone()["id"]
    p = tx.execute("insert into payers (merchant_id, display_name) values (%s, 'A') returning id", (m,)).fetchone()["id"]
    ins = ("insert into invoices (merchant_id, number, payer_id, amount_paise, due_date, paytm_order_id) "
           "values (%s, %s, %s, 10000, current_date, 'ORD-SAME')")
    tx.execute(ins, (m, "INV-9001", p))
    with pytest.raises(psycopg.errors.UniqueViolation):
        tx.execute(ins, (m, "INV-9002", p))


def test_money_rejects_non_positive_amounts(tx):
    m = tx.execute("insert into merchants (name) values ('T') returning id").fetchone()["id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        tx.execute("insert into transactions (merchant_id, amount_paise, paid_at, source) "
                   "values (%s, 0, now(), 'seed')", (m,))


def _index_defs(db, table: str) -> list[str]:
    return [r["indexdef"] for r in db.execute(
        "select indexdef from pg_indexes where schemaname = 'public' and tablename = %s", (table,))]


def test_transaction_indexes(db):
    defs = _index_defs(db, "transactions")
    assert any(re.search(r"\(merchant_id, paid_at\)", d) for d in defs), defs
    assert any(re.search(r"\(payer_id, paid_at\)", d) for d in defs), defs


def test_payer_name_trigram_index(db):
    assert db.execute("select 1 from pg_extension where extname = 'pg_trgm'").fetchone(), "pg_trgm missing"
    defs = _index_defs(db, "payers")
    assert any("gin" in d.lower() and "display_name" in d and "gin_trgm_ops" in d for d in defs), defs


def test_trigram_index_is_used_for_fuzzy_name_search(tx):
    tx.execute("set local enable_seqscan = off")
    plan = "\n".join(r["QUERY PLAN"] for r in tx.execute(
        "explain select id from payers where display_name % 'Raam'"))
    assert "payers_name_trgm_idx" in plan or "trgm" in plan, plan


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def test_views_are_security_invoker(db):
    for v in VIEWS:
        opts = db.execute(
            "select c.reloptions from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relname = %s and c.relkind = 'v'", (v,),
        ).fetchone()
        assert opts, f"view {v} missing"
        assert "security_invoker=true" in (opts["reloptions"] or []), f"{v} bypasses RLS"


def test_payer_stats_columns(db):
    """payer_stats has count, total, median amount, typical hour, last_paid_at and median gap in days."""
    cols = _columns(db, "payer_stats")
    for c in ("payer_id", "merchant_id", "txn_count", "total_paise", "median_amount_paise",
              "typical_hour", "last_paid_at", "median_gap_days"):
        assert c in cols, f"payer_stats lacks {c}"
    assert cols["last_paid_at"]["data_type"] == "timestamp with time zone"


def test_daily_totals_and_heatmap_columns(db):
    d = _columns(db, "daily_totals")
    assert {"merchant_id", "day", "total_paise", "txn_count"} <= set(d), sorted(d)
    h = _columns(db, "hourly_heatmap")
    assert {"merchant_id", "dow", "hour", "total_paise", "txn_count"} <= set(h), sorted(h)


# ---------------------------------------------------------------------------
# RLS
# ---------------------------------------------------------------------------
def test_rls_on_every_public_table(db):
    rows = db.execute(
        "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind in ('r', 'p')"
    ).fetchall()
    off = [r["relname"] for r in rows if not r["relrowsecurity"]]
    assert not off, f"RLS off on {off}"


def test_no_write_policies_for_client_roles(db):
    """All writes go through the API; no INSERT/UPDATE/DELETE policy for anon/authenticated."""
    rows = db.execute(
        "select tablename, policyname, cmd, roles from pg_policies where schemaname = 'public'"
    ).fetchall()
    writes = [r for r in rows if r["cmd"] not in ("SELECT",)]
    assert not writes, writes
    readable = {r["tablename"] for r in rows}
    for t in ("merchants", "payers", "payer_identifiers", "transactions", "invoices",
              "recurring_schedules", "reminders"):
        assert t in readable, f"no select policy on {t}"


def test_next_invoice_number_not_callable_by_clients(db):
    for role in ("anon", "authenticated"):
        ok = db.execute(
            "select has_function_privilege(%s, 'public.next_invoice_number(uuid)', 'execute') as ok", (role,)
        ).fetchone()["ok"]
        assert not ok, f"{role} can mint invoice numbers"


# ---------------------------------------------------------------------------
# Demo clock: app_clock(id=1, override_now?) — every job and query uses now() from here
# ---------------------------------------------------------------------------
def test_app_clock_is_singleton(tx):
    with pytest.raises(psycopg.errors.CheckViolation):
        tx.execute("insert into app_clock (id) values (2)")


def test_app_now_is_real_now_by_default(db):
    r = db.execute("select override_now, now() as now, app_now() as app from app_clock where id = 1").fetchone()
    assert r["override_now"] is None, "demo clock left jumped"
    assert abs((r["app"] - r["now"]).total_seconds()) < 1


def test_app_now_follows_override_now_alone(tx):
    """The spec's app_clock only has override_now. Setting it must move app_now()."""
    target = datetime(2026, 12, 1, 6, 0, tzinfo=timezone.utc)
    tx.execute("update app_clock set override_now = %s where id = 1", (target,))
    app = tx.execute("select app_now() as t").fetchone()["t"]
    assert abs((app - target).total_seconds()) < 60, (
        f"override_now={target.isoformat()} but app_now()={app.isoformat()} (set_at left NULL)"
    )


def test_app_now_keeps_flowing_after_jump(tx):
    target = datetime(2026, 12, 1, 6, 0, tzinfo=timezone.utc)
    tx.execute("update app_clock set override_now = %s, set_at = now() - interval '1 hour' where id = 1", (target,))
    app = tx.execute("select app_now() as t").fetchone()["t"]
    assert abs((app - (target + timedelta(hours=1))).total_seconds()) < 5


def test_app_today_uses_ist(tx):
    # 19:00 UTC on 30 Oct is 00:30 IST on 31 Oct.
    tx.execute("update app_clock set override_now = '2026-10-30 19:00:00+00', set_at = now() where id = 1")
    assert str(tx.execute("select app_today() as d").fetchone()["d"]) == "2026-10-31"


def test_heatmap_window_follows_demo_clock(tx, settings, seeded):
    m = tx.execute(
        "select m.id from merchants m join auth.users u on u.id = m.owner_user_id where u.email = %s",
        (settings.demo_merchant_email,),
    ).fetchone()["id"]
    before = tx.execute("select count(*) as n from hourly_heatmap where merchant_id = %s", (m,)).fetchone()["n"]
    assert before > 0
    tx.execute("update app_clock set override_now = now() + interval '60 days', set_at = now() where id = 1")
    after = tx.execute("select count(*) as n from hourly_heatmap where merchant_id = %s", (m,)).fetchone()["n"]
    assert after == 0, "hourly_heatmap ignores app_now()"


# ---------------------------------------------------------------------------
# Realtime and storage
# ---------------------------------------------------------------------------
def test_realtime_publishes_invoices_and_transactions(db):
    tables = {r["tablename"] for r in db.execute(
        "select tablename from pg_publication_tables where pubname = 'supabase_realtime' and schemaname = 'public'")}
    assert {"invoices", "transactions"} <= tables, tables


def test_qr_storage_bucket_exists_and_is_public(db):
    row = db.execute("select public from storage.buckets where id = 'qr'").fetchone()
    assert row and row["public"]
