"""Tester M1: the seeded demo ledger (ARCHITECTURE "Decisions/Data", "Low-friction ledger",
PLAN M1, pitch deck slides 4-6).

Reads the seed produced by the session `seeded` fixture. The demo merchant id is looked
up live by the owner's email so a concurrent re-seed does not leave a stale id.
"""

from __future__ import annotations

import re
from datetime import time, timedelta

import pytest

from app.money import IST


@pytest.fixture
def mid(db, settings, seeded):
    row = db.execute(
        "select m.id from merchants m join auth.users u on u.id = m.owner_user_id where u.email = %s",
        (settings.demo_merchant_email,),
    ).fetchone()
    assert row, "demo merchant not found"
    return row["id"]


def _now(db):
    return db.execute("select app_now() as now, app_today() as today").fetchone()


def _payer(db, mid, name):
    rows = db.execute("select * from payers where merchant_id = %s and display_name = %s", (mid, name)).fetchall()
    assert len(rows) == 1, f"expected exactly one payer named {name!r}, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------------------
# Merchant and volume
# ---------------------------------------------------------------------------
def test_demo_merchant_is_gupta_kirana_store(db, settings, mid):
    m = db.execute("select * from merchants where id = %s", (mid,)).fetchone()
    assert m["name"] == "Gupta Kirana Store"
    assert m["paytm_mid"], "merchant has no Paytm MID (mock MID expected in mock mode)"
    owners = db.execute(
        "select count(*) as n from merchants m join auth.users u on u.id = m.owner_user_id where u.email = %s",
        (settings.demo_merchant_email,),
    ).fetchone()["n"]
    assert owners == 1


def test_about_40_payers(db, mid):
    n = db.execute("select count(*) as n from payers where merchant_id = %s", (mid,)).fetchone()["n"]
    assert 35 <= n <= 45, n


def test_about_8k_rows(db, mid):
    n = db.execute("select count(*) as n from transactions where merchant_id = %s", (mid,)).fetchone()["n"]
    assert 7000 <= n <= 9000, n


def test_last_90_days_have_sales_every_day(db, mid):
    today = _now(db)["today"]
    days = {r["day"] for r in db.execute(
        "select day from daily_totals where merchant_id = %s and day > %s", (mid, today - timedelta(days=90)))}
    missing = [today - timedelta(days=k) for k in range(90) if today - timedelta(days=k) not in days]
    assert not missing, f"days with no sales: {missing[:5]}"
    walkin_first = db.execute(
        "select min((paid_at at time zone 'Asia/Kolkata')::date) as d from transactions "
        "where merchant_id = %s and payer_id is null", (mid,)).fetchone()["d"]
    assert 88 <= (today - walkin_first).days <= 92


# ---------------------------------------------------------------------------
# Sanity: no future rows, IST business hours, sane amounts, consistent keys
# ---------------------------------------------------------------------------
def test_no_future_dated_rows(db, mid):
    now = _now(db)["now"]
    checks = {
        "transactions.paid_at": "select max(paid_at) as t from transactions where merchant_id = %s",
        "transactions.created_at": "select max(created_at) as t from transactions where merchant_id = %s",
        "invoices.created_at": "select max(created_at) as t from invoices where merchant_id = %s",
        "invoices.sent_at": "select max(sent_at) as t from invoices where merchant_id = %s",
        "invoices.paid_at": "select max(paid_at) as t from invoices where merchant_id = %s",
        "reminders.sent_at": "select max(sent_at) as t from reminders where merchant_id = %s",
        "payers.created_at": "select max(created_at) as t from payers where merchant_id = %s",
    }
    for label, sql in checks.items():
        t = db.execute(sql, (mid,)).fetchone()["t"]
        assert t is None or t <= now, f"{label} is in the future: {t}"


def test_payments_fall_in_ist_shop_hours(db, mid):
    rows = db.execute(
        "select (paid_at at time zone 'Asia/Kolkata')::time as t, count(*) as n from transactions "
        "where merchant_id = %s and ((paid_at at time zone 'Asia/Kolkata')::time < '06:00' "
        "or (paid_at at time zone 'Asia/Kolkata')::time >= '23:00') group by 1", (mid,)).fetchall()
    assert not rows, f"payments outside 06:00-23:00 IST: {rows[:5]}"


def test_amounts_are_sane(db, mid):
    r = db.execute(
        "select min(amount_paise) as lo, max(amount_paise) as hi, "
        "max(amount_paise) filter (where payer_id is null) as walkin_hi, "
        "percentile_cont(0.5) within group (order by amount_paise) as med "
        "from transactions where merchant_id = %s", (mid,)).fetchone()
    assert r["lo"] >= 1_00, "payment below Rs 1"
    assert r["hi"] <= 25_000_00, f"implausibly large kirana payment: {r['hi']}"
    assert r["walkin_hi"] <= 5_000_00
    assert 50_00 <= r["med"] <= 500_00, f"median ticket {r['med']} paise"


def test_transactions_point_at_same_merchant_payers(db, mid):
    bad = db.execute(
        "select count(*) as n from transactions t join payers p on p.id = t.payer_id "
        "where t.merchant_id = %s and p.merchant_id <> t.merchant_id", (mid,)).fetchone()["n"]
    assert bad == 0


def test_every_payer_has_a_vpa_and_matching_identifiers(db, mid):
    rows = db.execute(
        "select p.display_name, p.email, p.phone, "
        "array_agg(i.kind || '=' || i.value) filter (where i.id is not null) as idents, "
        "bool_and(i.merchant_id = p.merchant_id) as same_merchant "
        "from payers p left join payer_identifiers i on i.payer_id = p.id "
        "where p.merchant_id = %s group by p.id", (mid,)).fetchall()
    for r in rows:
        idents = r["idents"] or []
        assert any(x.startswith("vpa=") for x in idents), f"{r['display_name']} has no VPA"
        assert r["same_merchant"]
        if r["email"]:
            assert f"email={r['email']}" in idents, r["display_name"]
        if r["phone"]:
            assert f"phone={r['phone']}" in idents, r["display_name"]


def test_every_payer_has_paid_at_least_once(db, mid):
    """Payer profiles are built from payments; a payer with no payment has no stats row."""
    rows = db.execute(
        "select p.display_name from payers p where p.merchant_id = %s "
        "and not exists (select 1 from transactions t where t.payer_id = p.id)", (mid,)).fetchall()
    assert not rows, f"payers with no payments: {[r['display_name'] for r in rows]}"


def test_views_agree_with_base_table(db, mid):
    base = db.execute("select sum(amount_paise) as s, count(*) as n from transactions where merchant_id = %s",
                      (mid,)).fetchone()
    daily = db.execute("select sum(total_paise) as s, sum(txn_count) as n from daily_totals where merchant_id = %s",
                       (mid,)).fetchone()
    assert (daily["s"], daily["n"]) == (base["s"], base["n"])
    named = db.execute(
        "select sum(amount_paise) as s from transactions where merchant_id = %s and payer_id is not null",
        (mid,)).fetchone()["s"]
    stats = db.execute("select sum(total_paise) as s from payer_stats where merchant_id = %s", (mid,)).fetchone()["s"]
    assert stats == named


def test_peak_hours_are_morning_and_evening(db, mid):
    rows = db.execute(
        "select hour, sum(txn_count) as n from hourly_heatmap where merchant_id = %s group by hour order by n desc",
        (mid,)).fetchall()
    assert rows
    top3 = {r["hour"] for r in rows[:3]}
    assert top3 & {7, 8, 9} and top3 & {18, 19, 20}, top3


# ---------------------------------------------------------------------------
# Ram: Rs 100 daily around 8:30, 121 payments in the last 4 months (slides 4-5)
# ---------------------------------------------------------------------------
def test_ram_profile(db, mid):
    ram = _payer(db, mid, "Ram")
    vpa = db.execute("select value from payer_identifiers where payer_id = %s and kind = 'vpa'", (ram["id"],)).fetchall()
    assert [r["value"] for r in vpa] == ["ram@paytm"]
    s = db.execute("select * from payer_stats where payer_id = %s", (ram["id"],)).fetchone()
    assert s["median_amount_paise"] == 100_00
    assert s["typical_hour"] == 8
    assert float(s["median_gap_days"]) == pytest.approx(1.0, abs=0.05)


def test_ram_four_month_answer_is_121_payments_12100(db, mid):
    ram = _payer(db, mid, "Ram")
    r = db.execute(
        "select count(*) as n, sum(amount_paise) as total, bool_and(amount_paise = 10000) as all100, "
        "min((paid_at at time zone 'Asia/Kolkata')::time) as earliest, "
        "max((paid_at at time zone 'Asia/Kolkata')::time) as latest from transactions where payer_id = %s "
        "and paid_at >= ((app_today() - interval '4 months')::date)::timestamp at time zone 'Asia/Kolkata' "
        "and paid_at <= app_now()", (ram["id"],)).fetchone()
    assert r["n"] == 121 and r["total"] == 12_100_00
    assert r["all100"]
    assert r["earliest"] >= time(8, 0) and r["latest"] <= time(9, 0), (r["earliest"], r["latest"])


def test_ram_last_payment_is_today_after_his_usual_time(db, mid):
    now = _now(db)["now"].astimezone(IST)
    if now.time() < time(9, 0):
        pytest.skip("before Ram's usual time")
    ram = _payer(db, mid, "Ram")
    last = db.execute("select max(paid_at) as t from transactions where payer_id = %s", (ram["id"],)).fetchone()["t"]
    assert last.astimezone(IST).date() == now.date(), "deck: 'Last payment: today'"


def test_fuzzy_search_finds_ram_and_sharma(db, mid):
    for query, expected in [("Raam", "Ram"), ("ram", "Ram"), ("sharma trader", "Sharma Traders"),
                            ("Priya Caterer", "Priya Caterers")]:
        top = db.execute(
            "select display_name from payers where merchant_id = %s order by similarity(display_name, %s) desc limit 1",
            (mid, query)).fetchone()["display_name"]
        assert top == expected, (query, top)


# ---------------------------------------------------------------------------
# Collections: slide 6
# ---------------------------------------------------------------------------
def _invoices(db, mid):
    return {r["number"]: r for r in db.execute(
        "select i.*, p.display_name, p.gstin as p_gstin, p.email as p_email from invoices i "
        "join payers p on p.id = i.payer_id where i.merchant_id = %s", (mid,))}


def test_sharma_traders_is_b2b_with_gstin(db, mid):
    s = _payer(db, mid, "Sharma Traders")
    assert s["gstin"] and re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", s["gstin"]), s["gstin"]
    assert s["email"] == "accounts@sharmatraders.in"
    invs = sorted((r for r in _invoices(db, mid).values() if r["display_name"] == "Sharma Traders"),
                  key=lambda r: r["created_at"])
    assert len(invs) >= 3, "Sharma Traders should have a B2B invoice history"
    for r in invs:
        assert r["gst_taxable"] and r["payer_gstin"] == s["gstin"], r["number"]


def test_inv_0412_sharma_8500_overdue_9_days(db, mid):
    inv = _invoices(db, mid)["INV-0412"]
    today = _now(db)["today"]
    assert inv["display_name"] == "Sharma Traders"
    assert inv["amount_paise"] == 8_500_00
    assert inv["status"] == "overdue"
    assert (today - inv["due_date"]).days == 9
    assert inv["paid_at"] is None
    assert inv["email_to"] == "accounts@sharmatraders.in"
    assert inv["sent_at"] is not None
    paid = db.execute("select count(*) as n from transactions where invoice_id = %s", (inv["id"],)).fetchone()["n"]
    assert paid == 0


def test_priya_caterers_3200_due_tomorrow(db, mid):
    today = _now(db)["today"]
    rows = [r for r in _invoices(db, mid).values()
            if r["display_name"] == "Priya Caterers" and r["status"] in ("sent", "overdue")]
    assert len(rows) == 1
    assert rows[0]["amount_paise"] == 3_200_00 and rows[0]["status"] == "sent"
    assert (rows[0]["due_date"] - today).days == 1


def test_outstanding_11700_across_two_customers(db, mid):
    open_rows = [r for r in _invoices(db, mid).values() if r["status"] in ("sent", "overdue")]
    assert sum(r["amount_paise"] for r in open_rows) == 11_700_00
    assert len({r["payer_id"] for r in open_rows}) == 2


def test_invoice_line_items_add_up(db, mid):
    for number, r in _invoices(db, mid).items():
        items = r["line_items"]
        assert items, f"{number} has no line items"
        for it in items:
            assert {"name", "qty", "unit_price_paise"} <= set(it), (number, it)
            assert isinstance(it["unit_price_paise"], int) and isinstance(it["qty"], int), (number, it)
        assert sum(it["qty"] * it["unit_price_paise"] for it in items) == r["amount_paise"], number


def test_invoice_numbers_and_sequence(db, mid):
    invs = _invoices(db, mid)
    assert all(re.fullmatch(r"INV-\d{4}", n) for n in invs)
    seq = db.execute("select invoice_seq from merchants where id = %s", (mid,)).fetchone()["invoice_seq"]
    assert seq == max(int(n[4:]) for n in invs), "next_invoice_number() would collide or skip"


def test_invoice_status_matches_dates(db, mid):
    today = _now(db)["today"]
    for n, r in _invoices(db, mid).items():
        due_plus_grace = r["due_date"] + timedelta(days=r["grace_days"])
        if r["status"] == "overdue":
            assert due_plus_grace < today, n
        if r["status"] == "sent":
            assert due_plus_grace >= today, n
        if r["status"] in ("sent", "overdue", "paid"):
            assert r["sent_at"] is not None, n
        assert r["created_at"] <= (r["sent_at"] or r["created_at"]), n


def test_paid_invoices_are_auto_matched_to_one_payment(db, mid):
    paid = [r for r in _invoices(db, mid).values() if r["status"] == "paid"]
    assert paid
    for r in paid:
        t = db.execute("select * from transactions where invoice_id = %s", (r["id"],)).fetchall()
        assert len(t) == 1, r["number"]
        t = t[0]
        assert t["amount_paise"] == r["amount_paise"] and t["payer_id"] == r["payer_id"], r["number"]
        assert t["paytm_order_id"] == r["paytm_order_id"] and t["paytm_txn_id"], r["number"]
        assert t["paid_at"] == r["paid_at"], r["number"]
        assert t["gst_taxable"] == r["gst_taxable"], r["number"]
    assert any(r["display_name"] != "Sharma Traders" and r["amount_paise"] == 1_500_00 for r in paid), \
        "slide 6: a Rs 1,500 invoice shown as Paid - auto-matched"


def test_open_invoices_have_a_reminder_history(db, mid):
    for r in _invoices(db, mid).values():
        if r["status"] in ("sent", "overdue"):
            n = db.execute("select count(*) as n from reminders where invoice_id = %s and merchant_id = %s",
                           (r["id"], mid)).fetchone()["n"]
            assert n >= 1, r["number"]


# ---------------------------------------------------------------------------
# Low-friction ledger rules
# ---------------------------------------------------------------------------
def test_exactly_one_lost_regular_who_stopped_12_days_ago(db, mid):
    """Lost-regular alert: now - last_paid_at > 3 x median gap and at least 10 payments."""
    rows = db.execute(
        "select p.display_name, ((s.last_paid_at at time zone 'Asia/Kolkata')::date) as last_day, s.txn_count "
        "from payer_stats s join payers p on p.id = s.payer_id where s.merchant_id = %s "
        "and s.txn_count >= 10 and app_now() - s.last_paid_at > 3 * s.median_gap_days * interval '1 day'",
        (mid,)).fetchall()
    assert len(rows) == 1, [r["display_name"] for r in rows]
    assert (_now(db)["today"] - rows[0]["last_day"]).days == 12, rows[0]


SLOW_DAY_SQL = """
with n as (select app_now() at time zone 'Asia/Kolkata' as now_ist),
d as (select ((select now_ist from n)::date - 7 * k) as day, k from generate_series(0, 4) k)
select d.k, coalesce(sum(t.amount_paise), 0)::bigint as paise
  from d left join transactions t
    on t.merchant_id = %s
   and (t.paid_at at time zone 'Asia/Kolkata')::date = d.day
   and (t.paid_at at time zone 'Asia/Kolkata')::time <= (select now_ist from n)::time
 group by d.k order by d.k
"""


def slow_day_ratio(db, mid) -> float:
    """today-so-far / average of the same weekday's previous 4 weeks to the same time."""
    rows = {r["k"]: r["paise"] for r in db.execute(SLOW_DAY_SQL, (mid,))}
    avg = sum(rows[k] for k in (1, 2, 3, 4)) / 4
    return rows[0] / avg if avg else 1.0


def test_today_is_a_slow_day_now(db, mid):
    """Slow-day banner: today up to this hour >= 30% below the same weekday's 4-week average."""
    ratio = slow_day_ratio(db, mid)
    assert ratio <= 0.70, f"today is only {100 * (1 - ratio):.1f}% below the 4-week same-weekday average"
