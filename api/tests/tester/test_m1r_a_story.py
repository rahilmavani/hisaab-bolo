"""Tester M1 (re-test): the seeded demo story on the session seed (real "now").

Spec: ARCHITECTURE "Decisions/Data" (Ram, Sharma Traders (B2B invoices), one regular who stopped
12 days ago), "Low-friction ledger" (lost-regular, slow-day), pitch deck slides 4-6.
The demo merchant id is looked up live so a re-seed by another module never leaves a stale id.
"""

from __future__ import annotations

from datetime import time

import pytest

from app.money import IST

from ..conftest import bearer


@pytest.fixture
def mid(db, settings, seeded):
    row = db.execute(
        "select m.id from merchants m join auth.users u on u.id = m.owner_user_id where u.email = %s",
        (settings.demo_merchant_email,),
    ).fetchone()
    assert row, "demo merchant not found"
    return row["id"]


def _now_ist(db):
    return db.execute("select app_now() as t").fetchone()["t"].astimezone(IST)


# ---------------------------------------------------------------------------
# B2B customers pay by invoice (ARCHITECTURE: "Sharma Traders (B2B invoices)"; seed notes:
# Sharma "invoiced fortnightly", Priya "pays by invoice")
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Sharma Traders", "Priya Caterers"])
def test_invoice_customers_only_pay_through_invoices(db, mid, name):
    rows = db.execute(
        "select (t.paid_at at time zone 'Asia/Kolkata') as paid_ist, t.amount_paise, t.description "
        "from transactions t join payers p on p.id = t.payer_id "
        "where p.merchant_id = %s and p.display_name = %s and t.invoice_id is null order by t.paid_at",
        (mid, name)).fetchall()
    assert not rows, (
        f"{name} has {len(rows)} payment(s) not tied to any invoice: "
        + ", ".join(f"{r['paid_ist']:%d %b %H:%M} Rs {r['amount_paise'] // 100} '{r['description']}'" for r in rows)
    )


def test_payers_exist_before_their_first_payment(db, mid):
    rows = db.execute(
        "select p.display_name, p.created_at, min(t.paid_at) as first_paid from payers p "
        "join transactions t on t.payer_id = p.id where p.merchant_id = %s group by p.id "
        "having p.created_at > min(t.paid_at)", (mid,)).fetchall()
    assert not rows, [(r["display_name"], r["created_at"], r["first_paid"]) for r in rows]


def test_meena_is_a_real_regular_before_she_stopped(db, mid):
    """The lapsed regular must look like a regular: >= 10 payments, steady short gaps, nothing after the stop."""
    s = db.execute(
        "select s.*, app_today() - (s.last_paid_at at time zone 'Asia/Kolkata')::date as days_since "
        "from payer_stats s join payers p on p.id = s.payer_id "
        "where p.merchant_id = %s and p.display_name = 'Meena Kapoor'", (mid,)).fetchone()
    assert s, "Meena Kapoor (the lapsed regular) missing"
    assert s["txn_count"] >= 10
    assert float(s["median_gap_days"]) <= 3
    assert s["days_since"] == 12


# ---------------------------------------------------------------------------
# The story as the app serves it (GET /dashboard/summary and /payers?q=ram read only seeded rows)
# ---------------------------------------------------------------------------
async def test_dashboard_tells_the_seeded_story(api, demo_token, db, mid):
    r = await api.get("/dashboard/summary", headers=bearer(demo_token))
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert [p["display_name"] for p in d["lost_regulars"]] == ["Meena Kapoor"], "Gone quiet list"
    out = d["outstanding"]
    assert out["total_paise"] == 11_700_00 and out["customers"] == 2
    by_name = {i["payer_name"]: i for i in out["invoices"]}
    assert by_name["Sharma Traders"]["number"] == "INV-0412" and by_name["Sharma Traders"]["days_overdue"] == 9
    assert by_name["Priya Caterers"]["amount_paise"] == 3_200_00 and by_name["Priya Caterers"]["days_overdue"] == -1
    if _now_ist(db).time() >= time(8, 0):
        assert d["slow_day"] and d["slow_day"]["is_slow"], d["slow_day"]


async def test_search_ram_shows_the_deck_card(api, demo_token):
    r = await api.get("/payers", params={"q": "ram"}, headers=bearer(demo_token))
    assert r.status_code == 200, r.text[:300]
    top = r.json()["payers"][0]
    assert top["display_name"] == "Ram" and top["vpa"] == "ram@paytm"
    assert top["txn_count"] == 121 and top["median_amount_paise"] == 100_00
    assert top["habit"].startswith("₹100 daily, ~8:"), top["habit"]
    assert "121 payments since" in top["habit"], top["habit"]
    assert "Regular" in top["badges"], top["badges"]
