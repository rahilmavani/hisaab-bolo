"""Ledger read models: dashboard KPIs, trends, payer profiles and search.

All functions take the merchant id explicitly; callers never trust a client-sent id.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID

from psycopg import AsyncConnection

from ..db import app_now, fetch_all, fetch_one
from ..money import IST, format_inr
from ..phones import india_digits, looks_like_phone

LOST_REGULAR_FACTOR = 3
LOST_REGULAR_MIN_PAYMENTS = 10
SLOW_DAY_DROP = 0.30
SLOW_DAY_MIN_BASELINE_PAISE = 500_00

Grain = Literal["day", "week", "month"]

PAYER_COLUMNS = """
    p.id, p.display_name, p.email, p.phone, p.gstin, p.notes, p.merged_into, p.created_at,
    coalesce(s.txn_count, 0) as txn_count, coalesce(s.total_paise, 0) as total_paise,
    s.median_amount_paise, s.typical_minute, s.first_paid_at, s.last_paid_at,
    s.median_gap_days::float as median_gap_days,
    (select i.value from payer_identifiers i where i.payer_id = p.id and i.kind = 'vpa'
      order by i.created_at, i.id limit 1) as vpa,
    exists (select 1 from recurring_schedules r where r.payer_id = p.id and r.active) as recurring
"""


# ---------------------------------------------------------------------------
# Habits and badges
# ---------------------------------------------------------------------------
def cadence_words(gap_days: float | None, count: int) -> str | None:
    if gap_days is None or count < 3:
        return None
    if gap_days <= 1.3:
        return "daily"
    if gap_days <= 2.5:
        return "every 2 days"
    if 6 <= gap_days <= 8.5:
        return "weekly"
    if 12 <= gap_days <= 16:
        return "fortnightly"
    if 26 <= gap_days <= 35:
        return "monthly"
    return f"every {round(gap_days)} days"


def clock_words(minute: int | None, step: int = 5) -> str | None:
    if minute is None:
        return None
    minute = int(round(minute / step) * step) % (24 * 60)
    h, m = divmod(minute, 60)
    suffix = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {suffix}"


def short_date(d: datetime | date) -> str:
    if isinstance(d, datetime):
        d = d.astimezone(IST).date()
    return f"{d.day} {d.strftime('%b')}"


def is_lost(row: dict, now: datetime) -> bool:
    gap = row.get("median_gap_days")
    last = row.get("last_paid_at")
    if not gap or not last or row.get("txn_count", 0) < LOST_REGULAR_MIN_PAYMENTS:
        return False
    return (now - last).total_seconds() / 86400 > LOST_REGULAR_FACTOR * gap


def enrich_payer(row: dict, now: datetime) -> dict:
    """Adds habit text ("₹100 daily, ~8:30 AM · 121 payments since 1 Jul") and badges."""
    count = row["txn_count"]
    gap = row.get("median_gap_days")
    lost = is_lost(row, now)
    parts = []
    cadence = cadence_words(gap, count)
    if row.get("median_amount_paise") and cadence:
        when = clock_words(row.get("typical_minute"))
        parts.append(f"{format_inr(row['median_amount_paise'])} {cadence}" + (f", ~{when}" if when else ""))
    if count and row.get("first_paid_at"):
        noun = "payment" if count == 1 else "payments"
        parts.append(f"{count} {noun} since {short_date(row['first_paid_at'])}")
    badges = []
    if row.get("recurring"):
        badges.append("Recurring")
    if lost:
        badges.append("Lapsed")
    elif count >= LOST_REGULAR_MIN_PAYMENTS and gap is not None and gap <= 8.5:
        badges.append("Regular")
    if row.get("first_paid_at") and (now - row["first_paid_at"]).days < 7:
        badges.append("New")
    out = dict(row)
    out["habit"] = " · ".join(parts) if parts else "No payments yet"
    out["typical_time"] = clock_words(row.get("typical_minute"))
    out["badges"] = badges
    out["lost"] = lost
    out["days_since_last"] = (
        (now.astimezone(IST).date() - row["last_paid_at"].astimezone(IST).date()).days if row.get("last_paid_at") else None
    )
    return out


# ---------------------------------------------------------------------------
# Payers
# ---------------------------------------------------------------------------
def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def phone_digits(text: str) -> str | None:
    """Digits to match against stored phones when the query looks like a phone number."""
    if not looks_like_phone(text):
        return None
    digits = india_digits(text)
    return digits if len(digits) >= 4 else None


async def search_payers(conn: AsyncConnection, merchant_id: UUID, q: str = "", limit: int = 50) -> list[dict]:
    q = q.strip()
    digits = phone_digits(q) or ""
    rows = await fetch_all(
        conn,
        f"""
        select {PAYER_COLUMNS},
               case when %(q)s = '' then 0
                    else greatest(similarity(p.display_name, %(q)s), word_similarity(%(q)s, p.display_name))
               end as score
          from payers p
          left join payer_stats s on s.payer_id = p.id
         where p.merchant_id = %(m)s and p.merged_into is null
           and (%(q)s = ''
                or p.display_name %% %(q)s
                or %(q)s <%% p.display_name
                or p.display_name ilike '%%' || %(like)s || '%%' escape '\\'
                or exists (select 1 from payer_identifiers i
                            where i.payer_id = p.id
                              and (i.value ilike '%%' || %(like)s || '%%' escape '\\'
                                   or (%(digits)s <> '' and i.kind = 'phone'
                                       and regexp_replace(i.value, '\\D', '', 'g') like '%%' || %(digits)s || '%%'))))
         order by score desc, coalesce(s.total_paise, 0) desc, p.display_name
         limit %(limit)s
        """,
        {"m": merchant_id, "q": q, "like": like_escape(q), "digits": digits, "limit": limit},
    )
    now = await app_now(conn)
    return [enrich_payer(r, now) for r in rows]


async def get_payer_row(conn: AsyncConnection, merchant_id: UUID, payer_id: UUID) -> dict | None:
    return await fetch_one(
        conn,
        f"select {PAYER_COLUMNS} from payers p left join payer_stats s on s.payer_id = p.id "
        "where p.merchant_id = %s and p.id = %s",
        (merchant_id, payer_id),
    )


async def payer_profile(conn: AsyncConnection, merchant_id: UUID, payer_id: UUID, txn_limit: int = 200) -> dict | None:
    row = await get_payer_row(conn, merchant_id, payer_id)
    if row is None:
        return None
    now = await app_now(conn)
    profile = enrich_payer(row, now)
    profile["identifiers"] = await fetch_all(
        conn,
        "select kind, value, created_at from payer_identifiers where payer_id = %s order by kind, created_at",
        (payer_id,),
    )
    profile["monthly"] = await fetch_all(
        conn,
        """
        select to_char(g.m, 'YYYY-MM') as month, coalesce(sum(t.amount_paise), 0)::bigint as total_paise,
               count(t.id)::int as txn_count
          from generate_series(date_trunc('month', (%(now)s::timestamptz at time zone 'Asia/Kolkata')) - interval '5 months',
                               date_trunc('month', (%(now)s::timestamptz at time zone 'Asia/Kolkata')),
                               interval '1 month') g(m)
          left join transactions t
            on t.payer_id = %(p)s
           and date_trunc('month', t.paid_at at time zone 'Asia/Kolkata') = g.m
         group by g.m order by g.m
        """,
        {"p": payer_id, "now": now},
    )
    profile["transactions"] = await list_transactions(conn, merchant_id, payer_id=payer_id, limit=txn_limit)
    profile["invoices"] = await fetch_all(
        conn,
        """
        select id, number, amount_paise, due_date, status, link_url, paid_at, sent_at, created_at, recurring_id,
               grace_days, (due_date - app_today()) as due_in_days
          from invoices where merchant_id = %s and payer_id = %s order by created_at desc
        """,
        (merchant_id, payer_id),
    )
    profile["recurring_suggestion"] = await recurring_suggestion(conn, payer_id, now) if not profile["recurring"] else None
    profile["recurring_schedule"] = await fetch_one(
        conn,
        """
        select id, amount_paise, cadence, anchor_date, next_due_date, grace_days, auto_remind, call_remind, active, email
          from recurring_schedules where payer_id = %s and active
        """,
        (payer_id,),
    )
    return profile


async def get_transaction(conn: AsyncConnection, merchant_id: UUID, txn_id: UUID) -> dict | None:
    rows = await list_transactions(conn, merchant_id, txn_id=txn_id, limit=1)
    return rows[0] if rows else None


async def recurring_suggestion(conn: AsyncConnection, payer_id: UUID, now: datetime) -> dict | None:
    """Stretch: a steady amount with a gap of about 1, 7 or 30 days suggests a recurring schedule."""
    row = await fetch_one(
        conn,
        """
        with t as (
          select amount_paise, extract(epoch from paid_at - lag(paid_at) over (order by paid_at)) / 86400.0 as gap
            from transactions where payer_id = %s and paid_at > %s::timestamptz - interval '90 days'
        )
        select count(*)::int as n, mode() within group (order by amount_paise) as amount,
               (percentile_cont(0.5) within group (order by gap))::float as gap
          from t
        """,
        (payer_id, now),
    )
    if not row or row["n"] < 6 or row["gap"] is None:
        return None
    share = await fetch_one(
        conn, "select avg((amount_paise = %s)::int)::float as s from transactions where payer_id = %s "
        "and paid_at > %s::timestamptz - interval '90 days'", (row["amount"], payer_id, now))
    if share["s"] < 0.7:
        return None
    gap = row["gap"]
    if gap <= 1.3:
        return {"cadence": "monthly", "every_days": 1, "amount_paise": row["amount"],
                "suggested_amount_paise": row["amount"] * 30}
    if 6 <= gap <= 8:
        return {"cadence": "weekly", "every_days": 7, "amount_paise": row["amount"],
                "suggested_amount_paise": row["amount"]}
    if 26 <= gap <= 35:
        return {"cadence": "monthly", "every_days": 30, "amount_paise": row["amount"],
                "suggested_amount_paise": row["amount"]}
    return None


async def list_transactions(
    conn: AsyncConnection,
    merchant_id: UUID,
    payer_id: UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    txn_id: UUID | None = None,
) -> list[dict]:
    return await fetch_all(
        conn,
        """
        select t.id, t.payer_id, p.display_name as payer_name, t.amount_paise, t.paid_at, t.source,
               t.payment_mode, t.description, t.description_by, t.category, t.invoice_id,
               i.number as invoice_number, t.paytm_order_id, t.gst_taxable
          from transactions t
          left join payers p on p.id = t.payer_id
          left join invoices i on i.id = t.invoice_id
         where t.merchant_id = %(m)s
           and (%(p)s::uuid is null or t.payer_id = %(p)s)
           and (%(t)s::uuid is null or t.id = %(t)s)
           and (%(start)s::timestamptz is null or t.paid_at >= %(start)s)
           and (%(end)s::timestamptz is null or t.paid_at < %(end)s)
         order by t.paid_at desc, t.id desc
         limit %(limit)s offset %(offset)s
        """,
        {"m": merchant_id, "p": payer_id, "t": txn_id, "start": start, "end": end,
         "limit": limit, "offset": offset},
    )


async def count_transactions(
    conn: AsyncConnection, merchant_id: UUID, payer_id: UUID | None = None,
    start: datetime | None = None, end: datetime | None = None,
) -> dict:
    row = await fetch_one(
        conn,
        """
        select count(*)::int as txn_count, coalesce(sum(amount_paise), 0)::bigint as total_paise
          from transactions
         where merchant_id = %(m)s
           and (%(p)s::uuid is null or payer_id = %(p)s)
           and (%(start)s::timestamptz is null or paid_at >= %(start)s)
           and (%(end)s::timestamptz is null or paid_at < %(end)s)
        """,
        {"m": merchant_id, "p": payer_id, "start": start, "end": end},
    )
    assert row
    return row


async def lost_regulars(conn: AsyncConnection, merchant_id: UUID) -> list[dict]:
    rows = await fetch_all(
        conn,
        f"""
        select {PAYER_COLUMNS}
          from payers p join payer_stats s on s.payer_id = p.id
         where p.merchant_id = %(m)s and p.merged_into is null
           and s.txn_count >= %(min)s
           and app_now() - s.last_paid_at > %(f)s * s.median_gap_days * interval '1 day'
         order by s.total_paise desc
        """,
        {"m": merchant_id, "min": LOST_REGULAR_MIN_PAYMENTS, "f": LOST_REGULAR_FACTOR},
    )
    now = await app_now(conn)
    return [enrich_payer(r, now) for r in rows]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def ist_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=IST)


async def day_kpis(conn: AsyncConnection, merchant_id: UUID, day: date, cutoff: datetime) -> dict:
    start = ist_midnight(day)
    row = await fetch_one(
        conn,
        """
        with d as (
          select t.payer_id, t.amount_paise
            from transactions t
           where t.merchant_id = %(m)s and t.paid_at >= %(start)s and t.paid_at <= %(cutoff)s
        ), firsts as (
          select payer_id, min(paid_at) as first_paid
            from transactions where merchant_id = %(m)s and payer_id is not null group by payer_id
        )
        select coalesce(sum(d.amount_paise), 0)::bigint as collected_paise,
               count(*)::int as payments,
               count(distinct d.payer_id) filter (where f.first_paid >= %(start)s)::int as new_payers,
               coalesce(sum(d.amount_paise) filter (where f.first_paid < %(start)s), 0)::bigint as repeat_paise
          from d left join firsts f on f.payer_id = d.payer_id
        """,
        {"m": merchant_id, "start": start, "cutoff": cutoff},
    )
    assert row
    row["repeat_share"] = (row["repeat_paise"] / row["collected_paise"]) if row["collected_paise"] else 0.0
    return row


def trend_start(end_day: date, days: int, grain: Grain) -> date:
    """First day of the window. Week/month grains start on a whole bucket (Monday / the 1st), so the
    first point is never a partial week or month; only the current, still-filling bucket is partial."""
    start = end_day - timedelta(days=days - 1)
    if grain == "week" and start.weekday():
        start += timedelta(days=7 - start.weekday())
    elif grain == "month" and start.day != 1:
        start = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
    return min(start, end_day)


async def trend(conn: AsyncConnection, merchant_id: UUID, end_day: date, days: int, grain: Grain,
                start: date | None = None) -> list[dict]:
    """Buckets ending at end_day. By default whole weeks/months only (the dashboard's chart); with an exact
    start, the first bucket is partial so the chart covers exactly start..end_day (the agent's totals)."""
    trunc = {"day": "day", "week": "week", "month": "month"}[grain]
    return await fetch_all(
        conn,
        f"""
        select greatest(date_trunc('{trunc}', g.day)::date, %(start)s::date) as bucket,
               coalesce(sum(dt.total_paise), 0)::bigint as total_paise,
               coalesce(sum(dt.txn_count), 0)::int as txn_count
          from generate_series(%(start)s::date, %(end)s::date, interval '1 day') g(day)
          left join daily_totals dt on dt.merchant_id = %(m)s and dt.day = g.day::date
         group by 1 order by 1
        """,
        {"m": merchant_id, "start": start or trend_start(end_day, days, grain), "end": end_day},
    )


async def heatmap(conn: AsyncConnection, merchant_id: UUID) -> list[dict]:
    return await fetch_all(
        conn,
        "select dow, hour, total_paise, txn_count from hourly_heatmap where merchant_id = %s order by dow, hour",
        (merchant_id,),
    )


async def slow_day(conn: AsyncConnection, merchant_id: UUID, now: datetime) -> dict:
    """Today so far vs the same weekday's 4-week average up to the same time of day."""
    today = now.astimezone(IST).date()
    rows = await fetch_all(
        conn,
        """
        select k, coalesce(sum(t.amount_paise), 0)::bigint as total_paise
          from generate_series(0, 4) k
          left join transactions t
            on t.merchant_id = %(m)s
           and t.paid_at >= ((%(today)s::date - 7 * k)::timestamp at time zone 'Asia/Kolkata')
           and t.paid_at <= %(now)s::timestamptz - (7 * k) * interval '1 day'
         group by k order by k
        """,
        {"m": merchant_id, "today": today, "now": now},
    )
    today_paise = rows[0]["total_paise"]
    baseline = round(sum(r["total_paise"] for r in rows[1:]) / 4 / 100) * 100  # whole rupees
    drop = 1 - today_paise / baseline if baseline else 0.0
    return {
        "is_slow": baseline >= SLOW_DAY_MIN_BASELINE_PAISE and drop >= SLOW_DAY_DROP,
        "today_paise": today_paise,
        "baseline_paise": baseline,
        "drop_pct": round(drop * 100),
        "weekday": today.strftime("%A"),
        "as_of": clock_words(now.astimezone(IST).hour * 60 + now.astimezone(IST).minute, step=1),
    }


async def outstanding(conn: AsyncConnection, merchant_id: UUID) -> dict:
    rows = await fetch_all(
        conn,
        """
        select i.id, i.number, i.payer_id, p.display_name as payer_name, i.amount_paise, i.due_date,
               i.grace_days, i.status, i.email_to, i.link_url, i.sent_at, i.created_at,
               (app_today() - i.due_date) as days_overdue,
               (select max(r.sent_at) from reminders r where r.invoice_id = i.id) as last_reminded_at
          from invoices i join payers p on p.id = i.payer_id
         where i.merchant_id = %s and i.status in ('sent', 'overdue')
         order by i.due_date, i.created_at
        """,
        (merchant_id,),
    )
    recently_paid = await fetch_all(
        conn,
        """
        select i.id, i.number, i.payer_id, p.display_name as payer_name, i.amount_paise, i.due_date,
               i.status, i.paid_at,
               exists (select 1 from transactions t where t.invoice_id = i.id and t.source = 'manual') as marked_paid
          from invoices i join payers p on p.id = i.payer_id
         where i.merchant_id = %s and i.status = 'paid' and i.paid_at > app_now() - interval '7 days'
         order by i.paid_at desc limit 3
        """,
        (merchant_id,),
    )
    return {
        "total_paise": sum(r["amount_paise"] for r in rows),
        "customers": len({r["payer_id"] for r in rows}),
        "invoices": rows,
        "recently_paid": recently_paid,
    }


async def dashboard_summary(conn: AsyncConnection, merchant_id: UUID, day: date | None) -> dict:
    now = await app_now(conn)
    today = now.astimezone(IST).date()
    day = day or today
    is_today = day == today
    cutoff = now if is_today else ist_midnight(day) + timedelta(days=1) - timedelta(microseconds=1)
    kpis = await day_kpis(conn, merchant_id, day, cutoff)
    prev = await day_kpis(conn, merchant_id, day - timedelta(days=7), cutoff - timedelta(days=7))
    change = (kpis["collected_paise"] / prev["collected_paise"] - 1) if prev["collected_paise"] else None
    hm = await heatmap(conn, merchant_id)
    by_hour: dict[int, int] = {}
    for cell in hm:
        by_hour[cell["hour"]] = by_hour.get(cell["hour"], 0) + cell["total_paise"]
    peak = sorted(by_hour.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return {
        "now": now,
        "date": day,
        "is_today": is_today,
        "kpis": {
            **kpis,
            "compare_day": day - timedelta(days=7),
            "compare_collected_paise": prev["collected_paise"],
            "change_vs_last_week": change,
        },
        "trend": await trend(conn, merchant_id, day, 7, "day"),
        "heatmap": hm,
        "peak_hours": [{"hour": h, "total_paise": v} for h, v in peak],
        "slow_day": await slow_day(conn, merchant_id, now) if is_today else None,
        "lost_regulars": await lost_regulars(conn, merchant_id),
        "recent": await list_transactions(conn, merchant_id, start=ist_midnight(day), end=cutoff, limit=8),
        "outstanding": await outstanding(conn, merchant_id),
    }
