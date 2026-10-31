"""Seed the demo merchant with ~90 days of synthetic Paytm history.

Run: `uv run python -m app.seed` (from api/). Re-running wipes and recreates the
demo merchant and resets the demo clock. Deterministic for a given "today".

Cast (from the pitch deck):
- Ram: ₹100 almost every morning around 8:30, 121 payments over the last 4 months.
- Sharma Traders: B2B buyer with GSTIN; fortnightly invoices; INV-0412 (₹8,500) overdue 9 days.
- Priya Caterers: ₹3,200 invoice due tomorrow.
- Anita Desai: ₹1,500 invoice paid and auto-matched.
- Meena Kapoor: a regular (every ~2 days) who stopped paying 12 days ago.
"""

from __future__ import annotations

import argparse
import math
import random
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .config import get_settings
from .money import IST

MERCHANT_NAME = "Gupta Kirana Store"
HISTORY_DAYS = 90
WALKIN_BASE_PER_DAY = 72
TODAY_WALKIN_FACTOR = 0.35  # today runs slow, so the slow-day banner has something to say
WEEKDAY_FACTOR = {0: 0.9, 1: 0.95, 2: 0.95, 3: 1.0, 4: 1.05, 5: 1.25, 6: 1.2}
# Walk-in traffic by IST hour: morning milk run and evening rush.
HOUR_WEIGHTS = {6: 2, 7: 7, 8: 10, 9: 9, 10: 6, 11: 4, 12: 4, 13: 3, 14: 2, 15: 2,
                16: 3, 17: 5, 18: 8, 19: 10, 20: 8, 21: 5, 22: 2}
PAYMENT_MODES = ["UPI"] * 17 + ["PPI", "PPI", "DC"]


@dataclass
class Payer:
    name: str
    vpa: str
    phone: str | None = None
    email: str | None = None
    gstin: str | None = None
    notes: str = ""
    every_days: float = 3
    amount: tuple[int, int] = (100, 400)  # rupees
    minute: int = 18 * 60  # preferred IST minute of day
    jitter_min: int = 25
    skip_prob: float = 0.1
    start_days_ago: int = HISTORY_DAYS
    stop_days_ago: int = 0
    descriptions: tuple[str, ...] = ("Daily groceries",)
    category: str = "Groceries"
    newcomer: bool = False
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class Txn:
    payer: Payer | None
    paid_at: datetime
    amount_paise: int
    description: str
    category: str
    payment_mode: str = "UPI"
    invoice_id: uuid.UUID | None = None
    paytm_order_id: str | None = None
    paytm_txn_id: str | None = None
    gst_taxable: bool = False


FIRST = ["Suresh", "Kavita", "Imran", "Rohit", "Neha", "Arjun", "Pooja", "Vikram", "Sunita",
         "Farhan", "Deepa", "Manoj", "Rekha", "Sanjay", "Asha", "Naveen", "Geeta", "Harish",
         "Lata", "Ajay", "Shabnam", "Prakash", "Usha", "Gopal", "Ritu", "Karan", "Seema",
         "Dinesh", "Nisha", "Bhavesh", "Zoya", "Ravi", "Tanvi", "Yusuf", "Hema"]
LAST = ["Yadav", "Iyer", "Shaikh", "Verma", "Joshi", "Nair", "Mehta", "Rao", "Pillai", "Khan",
        "Kulkarni", "Patil", "Bose", "Chopra", "Reddy", "Sinha", "Malhotra", "Pandey", "Das",
        "Ghosh", "Bhat", "Saxena", "Mishra", "Tiwari", "Agarwal", "Kaur", "Menon", "Dubey",
        "Thakur", "Gill", "Ansari", "Jain", "Shetty", "Qureshi", "Bhatt"]
VPA_HANDLES = ["okaxis", "ybl", "okhdfcbank", "paytm", "okicici", "ibl", "oksbi"]

REGULAR_PROFILES = [
    # every_days, amount range, minute, descriptions, category
    (1, (60, 140), 7 * 60 + 15, ("Morning milk and bread", "Milk, eggs and bread"), "Dairy"),
    (1, (40, 90), 6 * 60 + 50, ("Milk packets", "Milk and curd"), "Dairy"),
    (2, (150, 350), 19 * 60 + 10, ("Vegetables and eggs", "Evening groceries"), "Groceries"),
    (2, (120, 300), 8 * 60 + 40, ("Breakfast supplies", "Bread, butter and milk"), "Dairy"),
    (3, (300, 700), 18 * 60 + 30, ("Daily groceries", "Atta and dal top-up"), "Groceries"),
    (4, (400, 1200), 20 * 60, ("Groceries and snacks", "Household supplies"), "Groceries"),
    (5, (250, 800), 11 * 60, ("Cleaning supplies", "Soap and detergent"), "Household"),
    (7, (1500, 2600), 10 * 60 + 30, ("Weekly grocery run", "Weekly staples"), "Staples"),
    (7, (800, 1400), 17 * 60 + 45, ("Weekend snacks and drinks", "Party snacks"), "Snacks"),
    (10, (2000, 3500), 12 * 60, ("Monthly staples: rice, oil, dal",), "Staples"),
    (14, (2500, 4500), 16 * 60, ("Bulk staples order", "Fortnightly stock-up"), "Staples"),
]


def walkin_description(rng: random.Random, rupees: float, hour: int) -> tuple[str, str]:
    if rupees < 60:
        return rng.choice([("Tea and biscuits", "Snacks"), ("Bread and eggs", "Dairy"),
                           ("Single item purchase", "Groceries"), ("Chips and cold drink", "Snacks")])
    if rupees < 200:
        if hour < 11:
            return rng.choice([("Milk and bread", "Dairy"), ("Breakfast items", "Dairy"),
                               ("Milk, curd and paneer", "Dairy")])
        return rng.choice([("Snacks and cold drinks", "Snacks"), ("Vegetables and eggs", "Groceries"),
                           ("Small grocery top-up", "Groceries")])
    if rupees < 600:
        return rng.choice([("Daily groceries", "Groceries"), ("Atta and dal top-up", "Staples"),
                           ("Household cleaning supplies", "Household"), ("Cooking oil and spices", "Staples")])
    return rng.choice([("Weekly grocery run", "Groceries"), ("Monthly staples: rice, oil, dal", "Staples"),
                       ("Bulk household order", "Household")])


def at(day: date, minute: int) -> datetime:
    minute = max(6 * 60 + 15, min(22 * 60 + 45, minute))
    return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=IST)


GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def with_gstin_check(first14: str) -> str:
    """Append the GSTIN mod-36 check character."""
    total = 0
    for i, ch in enumerate(first14):
        product = GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return first14 + GSTIN_CHARS[(36 - total % 36) % 36]


def build_cast(rng: random.Random, today: date, now: datetime) -> list[Payer]:
    four_months_ago = add_months(today, -4)
    ram = Payer(
        name="Ram", vpa="ram@paytm", phone="9876543210",
        notes="Regular since summer. Pays every morning on the way to work.",
        every_days=1, amount=(100, 100), minute=8 * 60 + 30, jitter_min=12, skip_prob=0.0,
        start_days_ago=(today - four_months_ago).days - 1,
        descriptions=("Morning milk and bread",), category="Dairy",
    )
    sharma = Payer(
        name="Sharma Traders", vpa="sharmatraders@icici", phone="9820012345",
        email="accounts@sharmatraders.in", gstin=with_gstin_check("27AAPFS1234K1Z"),
        notes="B2B. Canteen supplies, invoiced fortnightly.", every_days=0,
    )
    priya = Payer(
        name="Priya Caterers", vpa="priyacaterers@okhdfcbank", phone="9811122233",
        email="orders@priyacaterers.in", notes="Catering orders, pays by invoice.",
        every_days=0,
    )
    anita = Payer(
        name="Anita Desai", vpa="anita.desai@okaxis", email="anita.desai@example.com",
        every_days=6, amount=(400, 900), minute=11 * 60, descriptions=("Weekly groceries",),
    )
    meena = Payer(
        name="Meena Kapoor", vpa="meena.kapoor@ybl", phone="9833344455",
        every_days=2, amount=(200, 450), minute=18 * 60 + 45, skip_prob=0.0, stop_days_ago=12,
        descriptions=("Evening groceries", "Vegetables and milk"),
    )
    cast = [ram, sharma, priya, anita, meena]

    used: set[str] = {p.name for p in cast}
    names = [f"{f} {l}" for f, l in zip(FIRST, rng.sample(LAST, len(LAST)))]
    for i, name in enumerate(names[:35]):
        if name in used:
            continue
        used.add(name)
        every, amount, minute, descs, cat = REGULAR_PROFILES[i % len(REGULAR_PROFILES)]
        first, last = name.lower().split()
        p = Payer(
            name=name,
            vpa=f"{first}.{last}{rng.randint(1, 99)}@{rng.choice(VPA_HANDLES)}",
            phone=f"9{rng.randint(100000000, 999999999)}" if rng.random() < 0.6 else None,
            email=f"{first}.{last}@example.com" if rng.random() < 0.3 else None,
            every_days=every, amount=amount, minute=minute + rng.randint(-40, 40),
            skip_prob=0.15 if every <= 2 else 0.05, descriptions=descs, category=cat,
            start_days_ago=HISTORY_DAYS - rng.randint(0, 20),
        )
        cast.append(p)
    # Newcomers: two first paid today, two earlier this week.
    now_minute = now.hour * 60 + now.minute
    too_early = now_minute < 10 * 60  # early on, a debut payment would swamp the slow-day baseline
    for p, start in zip(cast[-4:], (0, 0, 3, 5)):
        p.start_days_ago = 1 if start == 0 and too_early else start
        p.amount, p.every_days, p.category = (80, 300), 2, "Groceries"
        p.descriptions = ("Small grocery top-up", "Milk and snacks")
        p.skip_prob = 0.0
        p.newcomer = True
        if p.start_days_ago == 0:
            p.minute, p.jitter_min = min(p.minute, now_minute - 45), 5
    return cast


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    last_day = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, last_day))


def regular_txns(rng: random.Random, p: Payer, today: date, now: datetime) -> list[Txn]:
    out: list[Txn] = []
    if p.every_days <= 0:
        return out
    day = today - timedelta(days=p.start_days_ago)
    last_day = today - timedelta(days=p.stop_days_ago)
    while day <= last_day:
        skip_prob = 0.6 if day == today and not p.newcomer else p.skip_prob  # slow day
        if rng.random() >= skip_prob:
            when = at(day, int(rng.gauss(p.minute, p.jitter_min)))
            if when <= now:
                lo, hi = p.amount
                rupees = lo if lo == hi else round(rng.uniform(lo, hi) / 10) * 10
                out.append(Txn(p, when, rupees * 100, rng.choice(p.descriptions), p.category,
                               rng.choice(PAYMENT_MODES)))
        step = p.every_days if p.every_days < 2 else max(1, round(rng.gauss(p.every_days, p.every_days * 0.2)))
        day += timedelta(days=step)
    return out


def ram_txns(rng: random.Random, p: Payer, today: date, now: datetime) -> list[Txn]:
    """Exactly 121 payments of ₹100 when there is room, skipping a few random days."""
    start = today - timedelta(days=p.start_days_ago)
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    days = [d for d in days if at(d, p.minute + 15) <= now or d < today]
    skip = set(rng.sample(days[:-3], max(0, len(days) - 121))) if len(days) > 121 else set()
    out = []
    paying = [d for d in days if d not in skip]
    # Symmetric jitter around 8:30 so his median (the "usual time" on his card) is exactly 8:30.
    half = [rng.choice((0, 0, 0, 2, 4, 6, 9, 12)) for _ in range(len(paying) // 2)]
    offsets = half + [-o for o in half] + [0] * (len(paying) % 2)
    rng.shuffle(offsets)
    for d, off in zip(paying, offsets):
        when = at(d, p.minute + off)
        if when > now:
            when = now - timedelta(minutes=5)
        out.append(Txn(p, when, 100_00, "Morning milk and bread", "Dairy", "UPI"))
    # Fewer than 121 days in the window (February): a few evening top-ups make up the count.
    for d in rng.sample(days[:-1], max(0, 121 - len(out))):
        out.append(Txn(p, at(d, 19 * 60 + 10), 100_00, "Evening milk top-up", "Dairy", "UPI"))
    return out


def keep_regulars_current(rng: random.Random, cast: list[Payer], txns: list[Txn],
                          today: date, now: datetime) -> list[Txn]:
    """Only Meena should look lapsed: give any other regular whose random skips left
    a long gap a payment yesterday."""
    last: dict[str, datetime] = {}
    for t in txns:
        if t.payer and (t.payer.name not in last or t.paid_at > last[t.payer.name]):
            last[t.payer.name] = t.paid_at
    extra = []
    for p in cast:
        if p.every_days <= 0 or p.stop_days_ago or p.name not in last:
            continue
        if (now - last[p.name]).days >= max(2, p.every_days):
            when = at(today - timedelta(days=1), p.minute)
            lo, hi = p.amount
            extra.append(Txn(p, when, (round(rng.uniform(lo, hi) / 10) * 10) * 100,
                             rng.choice(p.descriptions), p.category, "UPI"))
    return extra


def pin_lapsed_regular(rng: random.Random, cast: list[Payer], txns: list[Txn], today: date) -> list[Txn]:
    """The lapsed regular's last payment lands exactly `stop_days_ago` days back."""
    out = list(txns)
    for p in cast:
        if not p.stop_days_ago:
            continue
        stop_day = today - timedelta(days=p.stop_days_ago)
        out = [t for t in out if not (t.payer is p and t.paid_at.astimezone(IST).date() >= stop_day)]
        lo, hi = p.amount
        out.append(Txn(p, at(stop_day, p.minute), (round(rng.uniform(lo, hi) / 10) * 10) * 100,
                       rng.choice(p.descriptions), p.category, "UPI"))
    return out


def ensure_everyone_paid(rng: random.Random, cast: list[Payer], txns: list[Txn], today: date) -> list[Txn]:
    """A payer exists because they paid: anyone left without a payment gets one yesterday."""
    paid = {id(t.payer) for t in txns if t.payer}
    extra = []
    for p in cast:
        if id(p) not in paid:
            lo, hi = p.amount
            extra.append(Txn(p, at(today - timedelta(days=1), p.minute),
                             (round(rng.uniform(lo, hi) / 10) * 10) * 100,
                             rng.choice(p.descriptions), p.category, "UPI"))
    return txns + extra


SLOW_DAY_TARGET = 0.5  # today so far at most half of the usual takings by this time


def make_today_slow(rng: random.Random, txns: list[Txn], today: date, now: datetime) -> list[Txn]:
    """Thin today's walk-ins and regulars (never Ram, a newcomer's first payment or invoice payments) until
    today-so-far is at most half the same weekday's 4-week average up to the same time,
    the same comparison the dashboard's slow-day banner makes."""
    def window(k: int) -> tuple[datetime, datetime]:
        day = today - timedelta(days=7 * k)
        return datetime.combine(day, time(0), tzinfo=IST), now - timedelta(days=7 * k)

    baseline = 0
    for k in range(1, 5):
        lo, hi = window(k)
        baseline += sum(t.amount_paise for t in txns if lo <= t.paid_at <= hi)
    baseline //= 4
    lo, hi = window(0)
    todays = [t for t in txns if lo <= t.paid_at <= hi]
    total = sum(t.amount_paise for t in todays)
    first_paid: dict[int, datetime] = {}
    for t in txns:
        if t.payer and (id(t.payer) not in first_paid or t.paid_at < first_paid[id(t.payer)]):
            first_paid[id(t.payer)] = t.paid_at
    removable = [t for t in todays if t.invoice_id is None
                 and not (t.payer and (t.payer.name == "Ram" or first_paid[id(t.payer)] == t.paid_at))]
    rng.shuffle(removable)
    drop: set[int] = set()
    while baseline and total > SLOW_DAY_TARGET * baseline and removable:
        t = removable.pop()
        drop.add(id(t))
        total -= t.amount_paise
    return [t for t in txns if id(t) not in drop]


def walkin_txns(rng: random.Random, today: date, now: datetime) -> list[Txn]:
    out: list[Txn] = []
    hours, weights = zip(*HOUR_WEIGHTS.items())
    for back in range(HISTORY_DAYS, -1, -1):
        day = today - timedelta(days=back)
        n = WALKIN_BASE_PER_DAY * WEEKDAY_FACTOR[day.weekday()] * rng.uniform(0.85, 1.15)
        if back == 0:
            n *= TODAY_WALKIN_FACTOR
        for _ in range(int(n)):
            hour = rng.choices(hours, weights)[0]
            when = datetime.combine(day, time(hour, rng.randint(0, 59), rng.randint(0, 59)), tzinfo=IST)
            if when > now:
                continue
            rupees = min(2500, max(10, math.exp(rng.gauss(math.log(110), 0.75))))
            rupees = round(rupees / 10) * 10 if rng.random() < 0.5 else round(rupees)
            desc, cat = walkin_description(rng, rupees, hour)
            out.append(Txn(None, when, int(rupees) * 100, desc, cat, rng.choice(PAYMENT_MODES)))
    return out


@dataclass
class Invoice:
    payer: Payer
    created: datetime
    due: date
    amount_rupees: int
    items: list[dict]
    status: str
    paid_at: datetime | None = None
    gst: bool = False
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    number: str = ""
    order_id: str = ""


def build_invoices(cast: list[Payer], today: date, now: datetime) -> list[Invoice]:
    by = {p.name: p for p in cast}
    sharma, priya, anita = by["Sharma Traders"], by["Priya Caterers"], by["Anita Desai"]

    def day(n: int, minute: int = 11 * 60) -> datetime:
        return at(today + timedelta(days=n), minute)

    def items(*rows: tuple[str, int, int]) -> list[dict]:
        return [{"name": n, "qty": q, "unit_price_paise": r * 100} for n, q, r in rows]

    invs = [
        Invoice(sharma, day(-86), (today + timedelta(days=-77)), 7200,
                items(("Canteen staples", 1, 5200), ("Tea and sugar", 4, 500)), "paid", day(-80, 15 * 60), True),
        Invoice(sharma, day(-72), (today + timedelta(days=-63)), 9800,
                items(("Canteen staples", 1, 7800), ("Cooking oil 15L", 1, 2000)), "paid", day(-65, 16 * 60), True),
        Invoice(priya, day(-65), (today + timedelta(days=-58)), 3200,
                items(("Catering order: dry goods", 1, 3200)), "paid", day(-60, 13 * 60)),
        Invoice(sharma, day(-58), (today + timedelta(days=-49)), 6500,
                items(("Canteen staples", 1, 6500)), "paid", day(-52, 12 * 60), True),
        Invoice(sharma, day(-44), (today + timedelta(days=-35)), 11250,
                items(("Canteen staples", 1, 8250), ("Festival sweets order", 1, 3000)), "paid", day(-36, 17 * 60), True),
        Invoice(priya, day(-35), (today + timedelta(days=-28)), 2800,
                items(("Catering order: dry goods", 1, 2800)), "paid", day(-30, 14 * 60)),
        Invoice(sharma, day(-30), (today + timedelta(days=-21)), 8000,
                items(("Canteen staples", 1, 8000)), "paid", day(-24, 11 * 60 + 30), True),
        Invoice(sharma, day(-18), (today + timedelta(days=-9)), 8500,
                items(("Canteen staples", 1, 6500), ("Tea and sugar", 4, 500)), "overdue", None, True),
        Invoice(anita, day(-10), (today + timedelta(days=-3)), 1500,
                items(("Diwali gift hampers", 3, 500)), "paid", day(-3, 10 * 60 + 20)),
        Invoice(priya, day(-6), (today + timedelta(days=1)), 3200,
                items(("Catering order: dry goods", 1, 2600), ("Disposable plates", 2, 300)), "sent"),
    ]
    # Numbers run in creation order and land Sharma's overdue invoice on INV-0412.
    first_number = 412 - 7
    for i, inv in enumerate(invs):
        n = first_number + i
        inv.number = f"INV-{n:04d}"
        inv.order_id = f"HB_INV{n:04d}_{secrets.token_hex(4).upper()}"
        if inv.paid_at and inv.paid_at > now:
            inv.paid_at = now - timedelta(minutes=30)
    return invs


def generate(now: datetime) -> tuple[list[Payer], list[Txn], list[Invoice]]:
    """Pure story generation (no DB): the cast, their payments and the invoices."""
    today = now.date()
    rng = random.Random(today.toordinal())
    cast = build_cast(rng, today, now)
    txns: list[Txn] = []
    for p in cast:
        txns += ram_txns(rng, p, today, now) if p.name == "Ram" else regular_txns(rng, p, today, now)
    txns += walkin_txns(rng, today, now)

    txns = pin_lapsed_regular(rng, cast, txns, today)

    invoices = build_invoices(cast, today, now)
    for inv in invoices:
        if inv.status == "paid":
            assert inv.paid_at
            txns.append(Txn(inv.payer, inv.paid_at, inv.amount_rupees * 100,
                            f"Invoice {inv.number} payment", "Bulk order", "UPI",
                            invoice_id=inv.id, paytm_order_id=inv.order_id,
                            paytm_txn_id=f"SEED{secrets.token_hex(10).upper()}", gst_taxable=inv.gst))
    # Order matters: thin today first, then backfill regulars (yesterday) and anyone unpaid.
    txns = make_today_slow(rng, txns, today, now)
    txns += keep_regulars_current(rng, cast, txns, today, now)
    txns = ensure_everyone_paid(rng, cast, txns, today)
    return cast, txns, invoices


def ensure_demo_user(conn: psycopg.Connection, s) -> uuid.UUID:
    row = conn.execute("select id from auth.users where email = %s", (s.demo_merchant_email,)).fetchone()
    headers = {"apikey": s.supabase_secret_key, "Authorization": f"Bearer {s.supabase_secret_key}"}
    if row:
        user_id = row[0]
        r = httpx.put(f"{s.supabase_url}/auth/v1/admin/users/{user_id}", headers=headers,
                      json={"password": s.demo_merchant_password, "email_confirm": True}, timeout=20)
        r.raise_for_status()
        return user_id
    r = httpx.post(f"{s.supabase_url}/auth/v1/admin/users", headers=headers, timeout=20, json={
        "email": s.demo_merchant_email,
        "password": s.demo_merchant_password,
        "email_confirm": True,
        "user_metadata": {"merchant_name": MERCHANT_NAME},
    })
    r.raise_for_status()
    return uuid.UUID(r.json()["id"])


def seed(now: datetime | None = None, verbose: bool = True) -> dict:
    s = get_settings()
    with psycopg.connect(s.database_url, autocommit=False) as conn:
        user_id = ensure_demo_user(conn, s)
        conn.execute("update app_clock set override_now = null, set_at = null where id = 1")
        if now is None:
            now = conn.execute("select now()").fetchone()[0]
        now = now.astimezone(IST)
        today = now.date()

        conn.execute("delete from merchants where owner_user_id = %s", (user_id,))
        merchant_id = conn.execute(
            "insert into merchants (name, owner_user_id, paytm_mid) values (%s, %s, %s) returning id",
            (MERCHANT_NAME, user_id, s.paytm_mid_active),
        ).fetchone()[0]

        cast, txns, invoices = generate(now)

        with conn.cursor() as cur:
            cur.executemany(
                "insert into payers (id, merchant_id, display_name, email, phone, gstin, notes, created_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(p.id, merchant_id, p.name, p.email, p.phone, p.gstin, p.notes,
                  at(today - timedelta(days=p.start_days_ago), 6 * 60 + 15)) for p in cast],
            )
            idents = []
            for p in cast:
                idents.append((p.id, merchant_id, "vpa", p.vpa))
                if p.phone:
                    idents.append((p.id, merchant_id, "phone", p.phone))
                if p.email:
                    idents.append((p.id, merchant_id, "email", p.email))
            cur.executemany(
                "insert into payer_identifiers (payer_id, merchant_id, kind, value) values (%s, %s, %s, %s)",
                idents,
            )
            cur.executemany(
                "insert into invoices (id, merchant_id, number, payer_id, amount_paise, line_items, gst_taxable, "
                "payer_gstin, due_date, status, email_to, paytm_order_id, sent_at, paid_at, created_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(inv.id, merchant_id, inv.number, inv.payer.id, inv.amount_rupees * 100, Jsonb(inv.items),
                  inv.gst, inv.payer.gstin if inv.gst else None, inv.due, inv.status, inv.payer.email,
                  inv.order_id, inv.created, inv.paid_at, inv.created) for inv in invoices],
            )
            cur.execute("update merchants set invoice_seq = %s where id = %s",
                        (int(invoices[-1].number.split("-")[1]), merchant_id))
            with cur.copy(
                "copy transactions (merchant_id, payer_id, amount_paise, paid_at, source, paytm_order_id, "
                "paytm_txn_id, payment_mode, description, category, gst_taxable, invoice_id) from stdin"
            ) as copy:
                for t in sorted(txns, key=lambda t: t.paid_at):
                    copy.write_row((merchant_id, t.payer.id if t.payer else None, t.amount_paise, t.paid_at,
                                    "seed", t.paytm_order_id, t.paytm_txn_id, t.payment_mode,
                                    t.description, t.category, t.gst_taxable, t.invoice_id))
            # Reminder history for the overdue invoice (the original invoice email).
            for inv in invoices:
                if inv.status in ("sent", "overdue"):
                    cur.execute(
                        "insert into reminders (merchant_id, invoice_id, kind, to_email, sent_at) "
                        "values (%s, %s, 'invoice', %s, %s)",
                        (merchant_id, inv.id, inv.payer.email, inv.created),
                    )
        conn.commit()

    summary = {
        "merchant_id": str(merchant_id),
        "payers": len(cast),
        "transactions": len(txns),
        "invoices": len(invoices),
        "today": today.isoformat(),
        "ram_payments": sum(1 for t in txns if t.payer and t.payer.name == "Ram"),
    }
    if verbose:
        print(summary)
    return summary


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    seed()
