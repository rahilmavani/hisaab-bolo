"""Tester M1 (final re-test): the seeded story must hold whatever time the seed runs on demo day.

Part 1 sweeps the pure generator `app.seed.generate(now)` (no DB) over ~4 months of instants.
Part 2 seeds the DB at one instant the sweep flagged (Sat 31 Oct 2026 12:00 IST), points the
demo clock there, reads what the app serves, then restores a fresh real-time seed.

Spec: ARCHITECTURE "Decisions/Data" (Ram Rs 100 daily around 8:30; one regular who stopped
12 days ago), deck slide 4 ("Ram · ram@paytm / Rs 100 daily, ~8:30 AM · 121 payments").
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

import psycopg
import pytest

from app.money import IST
from app.seed import add_months, generate, seed
from app.services.ledger import cadence_words, clock_words

from ..conftest import bearer, sign_in

START = date(2026, 10, 30)
DAYS = 120


def _median(vals):
    v = sorted(vals)
    n = len(v)
    k = (n - 1) / 2
    f = int(k)
    c = min(f + 1, n - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def _story(now: datetime) -> dict:
    today = now.date()
    cast, txns, invoices = generate(now)
    by = defaultdict(list)
    for t in txns:
        if t.payer:
            by[t.payer.name].append(t)
    stats = {}
    for name, ts in by.items():
        ts.sort(key=lambda t: t.paid_at)
        gaps = [(b.paid_at - a.paid_at).total_seconds() / 86400 for a, b in zip(ts, ts[1:])]
        mins = [t.paid_at.astimezone(IST).hour * 60 + t.paid_at.astimezone(IST).minute for t in ts]
        stats[name] = {"n": len(ts), "gap": round(_median(gaps), 2) if gaps else None, "last": ts[-1].paid_at,
                       "minute": round(_median(mins)), "amount": round(_median([t.amount_paise for t in ts]))}
    four = datetime.combine(add_months(today, -4), time(0), tzinfo=IST)
    return {"cast": cast, "txns": txns, "invoices": invoices, "stats": stats, "by": by, "four": four, "today": today}


SWEEP = [datetime.combine(START + timedelta(days=d), time(h, m), tzinfo=IST)
         for d in range(DAYS) for h, m in ((9, 30), (12, 0), (20, 30))]


@pytest.fixture(scope="module")
def stories():
    return {now: _story(now) for now in SWEEP}


def _fails(stories, check):
    bad = []
    for now, s in stories.items():
        msg = check(now, s)
        if msg:
            bad.append(f"{now:%a %d %b %Y %H:%M}: {msg}")
    return bad


def test_sweep_ram_habit_reads_like_the_deck(stories):
    """Deck slide 4: 'Rs 100 daily, ~8:30 AM · 121 payments since <date>'."""
    def check(now, s):
        r = s["stats"]["Ram"]
        words = (cadence_words(r["gap"], r["n"]), clock_words(r["minute"]), r["amount"], r["n"])
        return None if words == ("daily", "8:30 AM", 100_00, 121) else f"Ram habit {words}"
    bad = _fails(stories, check)
    assert not bad, f"{len(bad)} of {len(stories)} instants:\n" + "\n".join(bad)


def test_sweep_ram_121_payments_all_in_last_4_months(stories):
    def check(now, s):
        ts = s["by"]["Ram"]
        inside = sum(1 for t in ts if t.paid_at >= s["four"])
        return None if (len(ts), inside) == (121, 121) else f"Ram {len(ts)} payments, {inside} in last 4 months"
    assert not _fails(stories, check)


def test_sweep_exactly_one_lost_regular_12_days(stories):
    def check(now, s):
        lost = [n for n, st in s["stats"].items() if st["n"] >= 10 and st["gap"]
                and (now - st["last"]).total_seconds() / 86400 > 3 * st["gap"]]
        if lost != ["Meena Kapoor"]:
            return f"lost regulars {lost}"
        days = (s["today"] - s["stats"]["Meena Kapoor"]["last"].astimezone(IST).date()).days
        return None if days == 12 else f"Meena last paid {days} calendar days ago"
    bad = _fails(stories, check)
    assert not bad, "\n".join(bad[:20])


def test_sweep_slow_day_and_collections_story(stories):
    def check(now, s):
        tot = []
        for k in range(5):
            lo = datetime.combine(s["today"] - timedelta(days=7 * k), time(0), tzinfo=IST)
            tot.append(sum(t.amount_paise for t in s["txns"] if lo <= t.paid_at <= now - timedelta(days=7 * k)))
        base = sum(tot[1:]) / 4
        if not (base >= 500_00 and tot[0] <= 0.7 * base):
            return f"not a slow day: today {tot[0]} vs baseline {base:.0f}"
        open_ = [i for i in s["invoices"] if i.status in ("sent", "overdue")]
        if sum(i.amount_rupees for i in open_) != 11_700 or {i.payer.name for i in open_} != {"Sharma Traders", "Priya Caterers"}:
            return "outstanding story"
        if any(t.paid_at > now for t in s["txns"]):
            return "payments in the future"
        return None
    bad = _fails(stories, check)
    assert not bad, "\n".join(bad[:20])


# ---------------------------------------------------------------------------
# Part 2: one flagged instant, through the DB and the API.
# ---------------------------------------------------------------------------
FLAGGED = datetime(2026, 10, 31, 12, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def flagged_seed(settings):
    seed(now=FLAGGED, verbose=False)
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("update app_clock set override_now = %s, set_at = now() where id = 1", (FLAGGED,))
    yield sign_in(settings, settings.demo_merchant_email, settings.demo_merchant_password)
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("update app_clock set override_now = null, set_at = null where id = 1")
    seed(verbose=False)


async def test_flagged_instant_ram_card(api, flagged_seed):
    p = (await api.get("/payers", params={"q": "ram"}, headers=bearer(flagged_seed))).json()["payers"][0]
    assert p["habit"].startswith("₹100 daily, ~8:30 AM · 121 payments since"), p["habit"]


async def test_flagged_instant_gone_quiet_days(api, flagged_seed):
    """Dashboard 'Gone quiet' renders floor(days_since_last); the spec's regular stopped 12 days ago."""
    d = (await api.get("/dashboard/summary", headers=bearer(flagged_seed))).json()
    meena = next(p for p in d["lost_regulars"] if p["display_name"] == "Meena Kapoor")
    last_day = datetime.fromisoformat(meena["last_paid_at"]).astimezone(IST).date()
    assert (FLAGGED.date() - last_day).days == 12
    assert int(meena["days_since_last"]) == 12, (
        f"days_since_last={meena['days_since_last']:.2f} -> UI shows 'last paid {int(meena['days_since_last'])} days ago' "
        f"while Last paid is {last_day:%d %b}")
