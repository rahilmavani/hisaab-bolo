"""Typed agent tools. merchant_id is injected server-side and is never a parameter.

Read tools return compact JSON (amounts in rupees) and register their full result
under a result_id; the model picks a component type + result_id and the server
fills the rows. Write tools never write: they create a pending action that the
merchant confirms with a tap (POST /actions/{token}/confirm).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ...db import app_now, app_today, fetch_all, fetch_one
from ...money import IST, format_inr, paise_to_rupees, rupees_to_paise
from .. import ledger

PENDING_TTL = timedelta(minutes=15)


def _strict(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": properties, "required": required,
                         "additionalProperties": False},
    }


DATE = {"type": "string", "description": "Calendar date in IST, YYYY-MM-DD"}


def nullable(description: str) -> dict:
    return {"anyOf": [{"type": "string"}, {"type": "null"}], "description": description}

TOOLS: list[dict] = [
    _strict("find_payer", "Find customers by name, UPI ID or phone (fuzzy; speech-to-text may misspell names). "
            "Returns up to 5 matches with their payer_id. Call this before any payer-specific tool.",
            {"name": {"type": "string"}}, ["name"]),
    _strict("get_payer_summary", "One customer's payments between two dates (inclusive): count, total, usual amount "
            "and time, first and last payment in the window, plus lifetime habit. Component: payer_card / stat_cards.",
            {"payer_id": {"type": "string"}, "from": DATE, "to": DATE}, ["payer_id", "from", "to"]),
    _strict("list_transactions", "Individual payments between two dates (inclusive), newest first, optionally for "
            "one customer. Also returns the count and total for the whole window. Component: table.",
            {"payer_id": nullable("payer_id from find_payer, or null for all customers"), "from": DATE, "to": DATE,
             "limit": {"type": "integer", "description": "Rows to return, 1-200"}},
            ["payer_id", "from", "to", "limit"]),
    _strict("get_sales_summary", "Shop takings between two dates (inclusive) by day, week or month: totals, "
            "payment count, average per day, best day and peak hour. Components: line_chart + stat_cards.",
            {"from": DATE, "to": DATE, "grain": {"type": "string", "enum": ["day", "week", "month"]}},
            ["from", "to", "grain"]),
    _strict("list_dues", "Invoices by status: open (sent + overdue), overdue, paid or all. Component: table.",
            {"status": {"type": "string", "enum": ["open", "overdue", "paid", "all"]}}, ["status"]),
    _strict("create_invoice", "Prepare an invoice with a Paytm link and QR for a customer. Does NOT create it: "
            "returns a pending action the merchant must confirm. Component: confirm_action.",
            {"payer_id": {"type": "string"},
             "amount": {"type": "number", "description": "Total in rupees"},
             "due_date": DATE,
             "items": {"type": "array", "items": {"type": "string"},
                       "description": "What the invoice is for, one entry per line; may be empty"},
             "email": nullable("Customer email if the merchant gave one, else null")},
            ["payer_id", "amount", "due_date", "items", "email"]),
    _strict("send_reminder", "Prepare a payment reminder email (same Paytm link and QR) for an unpaid invoice. "
            "Returns a pending action the merchant must confirm. Component: confirm_action.",
            {"invoice_id": {"type": "string"}}, ["invoice_id"]),
    _strict("set_recurring", "Prepare marking a customer as recurring (weekly or monthly dues; with an email, "
            "invoices and overdue reminders go out automatically). Returns a pending action to confirm.",
            {"payer_id": {"type": "string"}, "cadence": {"type": "string", "enum": ["weekly", "monthly"]},
             "amount": {"type": "number", "description": "Amount per due, in rupees"},
             "email": nullable("Customer email if the merchant gave one, else null")},
            ["payer_id", "cadence", "amount", "email"]),
]

TOOL_NAMES = {t["name"] for t in TOOLS}
WRITE_TOOLS = {"create_invoice", "send_reminder", "set_recurring"}


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    conn: AsyncConnection
    merchant_id: UUID
    results: dict[str, dict] = field(default_factory=dict)  # result_id -> {"tool", "data"}
    pending: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    picked: str | None = None  # payer_id the merchant tapped on a chip for this question
    # Customers that only resembled a name the merchant asked about: off limits for this question
    # unless the merchant picks one (so "Ramesh" can't turn into Ram via a second search).
    lookalikes: set[str] = field(default_factory=set)

    def register(self, tool: str, data: dict) -> str:
        rid = f"r{len(self.results) + 1}"
        self.results[rid] = {"tool": tool, "data": data}
        return rid


def _rs(paise: int | None) -> float | None:
    if paise is None:
        return None
    r = paise_to_rupees(paise)
    return int(r) if r == int(r) else float(r)


def _date(s: str | None, field_name: str) -> date | None:
    if s is None:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise ToolError(f"{field_name} must be YYYY-MM-DD") from e


def _window(frm: str, to: str) -> tuple[date, date, datetime, datetime]:
    d1, d2 = _date(frm, "from"), _date(to, "to")
    if d1 > d2:
        raise ToolError("from must be on or before to")
    start = datetime(d1.year, d1.month, d1.day, tzinfo=IST)
    end = datetime(d2.year, d2.month, d2.day, tzinfo=IST) + timedelta(days=1)
    return d1, d2, start, end


async def _payer(ctx: ToolContext, payer_id: str) -> dict:
    try:
        pid = UUID(payer_id)
    except ValueError as e:
        raise ToolError("payer_id must come from find_payer") from e
    row = await fetch_one(ctx.conn, "select * from payers where id = %s and merchant_id = %s", (pid, ctx.merchant_id))
    if row is None:
        raise ToolError("no such customer")
    if str(pid) in ctx.lookalikes and str(pid) != ctx.picked:
        raise ToolError("the merchant asked about a different name; this customer only looks similar. Ask which "
                        "customer they mean (show the chips) instead of answering about this one")
    if row["merged_into"]:
        row = await fetch_one(ctx.conn, "select * from payers where id = %s", (row["merged_into"],))
    return row


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
# Speech-to-text often writes names in Devanagari; payer names are stored in Roman script.
_VOWELS = {"अ": "a", "आ": "a", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऋ": "ri"}
_MATRAS = {"ा": "a", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ृ": "ri"}
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "क़": "q", "ज़": "z", "फ़": "f", "ड़": "r", "ढ़": "rh",
}
_NASAL = {"ं": "n", "ँ": "n", "ः": "h"}


def romanize(text: str) -> str:
    """Rough Devanagari → Roman for name search (राम → ram, शर्मा → sharma, मीना → meena)."""
    out, chars = [], list(text)
    for i, ch in enumerate(chars):
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if ch in _CONSONANTS:
            out.append(_CONSONANTS[ch])
            word_end = not nxt or not ("\u0900" <= nxt <= "\u097f")
            # Inherent vowel, except word-finally where Hindi drops it (राम → ram, not rama).
            if nxt not in _MATRAS and nxt not in ("्", *_NASAL) and not word_end:
                out.append("a")
            elif nxt in _NASAL:
                out.append("a")
        elif ch in _MATRAS:
            out.append(_MATRAS[ch])
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _NASAL:
            out.append(_NASAL[ch])
        elif ch == "्":
            continue
        else:
            out.append(ch)
    return "".join(out)


def _has_devanagari(text: str) -> bool:
    return any("\u0900" <= ch <= "\u097f" for ch in text)


# Spoken forms of address: "Ram ji", "Sharma bhai", "Meena ben", "Mr Ram" are Ram, Sharma, Meena.
HONORIFICS = {"ji", "jee", "bhai", "bhaiya", "bhaiyya", "bhaisaab", "bhaisahab", "sahab", "saheb", "sir", "madam",
              "didi", "ben", "behen", "bahen", "aunty", "auntie", "uncle", "chacha", "mr", "mrs", "ms", "shri",
              "shree", "smt", "seth"}


def _without_honorifics(name: str) -> str:
    words = [w for w in name.replace(".", " ").split() if w.lower() not in HONORIFICS]
    return " ".join(words) or name


JOINED_HONORIFICS = ("bhaiya", "bhai", "behen", "didi", "sahab", "saab", "ben", "jee", "ji")


def _without_joined_ji(name: str) -> str:
    """"Ramji", "Rambhai", "Meenaben" -> Ram, Ram, Meena (the stem must keep 3+ letters)."""
    def strip(w: str) -> str:
        for h in JOINED_HONORIFICS:
            if w.lower().endswith(h) and len(w) - len(h) >= 3:
                return w[: -len(h)]
        return w
    return " ".join(strip(w) for w in name.split())


async def find_payer(ctx: ToolContext, name: str) -> dict:
    said = name
    if _has_devanagari(name):
        name = romanize(name)
    name = _without_honorifics(name)
    # "रामजी" romanizes to "ramajee", so a joined जी comes off before romanizing.
    joined = (romanize(re.sub(r"(?<=[\u0900-\u097F]{2})(?:जी|भाई|भैया|बेन|बहन|दीदी|साहब)(?=\s|$)", "", said))
              if _has_devanagari(said) else name)
    joined = _without_joined_ji(_without_honorifics(joined))
    # A fuzzy hit is only "the" customer when the asked-for name is really theirs: "Ramesh" must not
    # become Ram just because Ram is the closest name in the shop. A joined honorific ("Ramji") is tried
    # without its "ji" only if the name as said matches nobody (so "Balaji Stores" stays Balaji).
    for query in dict.fromkeys([name, joined]):
        rows = await ledger.search_payers(ctx.conn, ctx.merchant_id, query, limit=5)
        rows = [r for r in rows if r.get("score", 1) >= 0.25 or len(rows) == 1] or rows[:3]
        named = [r for r in rows if _is_their_name(query, r)]
        if named:
            name = query
            break
    matches = [{"payer_id": str(r["id"]), "name": r["display_name"], "vpa": r["vpa"], "habit": r["habit"],
                "payments": r["txn_count"], "total_rupees": _rs(r["total_paise"])} for r in rows]
    out = {"matches": matches, "single_match": len(named) == 1}
    if matches and not named:
        ctx.lookalikes |= {m["payer_id"] for m in matches}
        out["no_exact_match"] = True
        out["note"] = (f"No customer is named {name!r}; these only look similar. Ask which one is meant (or whether "
                       "it is someone new) and say nothing about their payments.")
    return out


def _is_their_name(query: str, row: dict) -> bool:
    """Every word of the query is a word (or the start of a word, 3+ letters) of the customer's name; or the
    query is a phone number / UPI ID they were found by; or the trigram match is strong."""
    if row.get("score", 0) >= 0.6 or "@" in query or sum(ch.isdigit() for ch in query) >= 6:
        return True
    q = re.findall(r"[a-z0-9]+", query.lower())
    words = re.findall(r"[a-z0-9]+", row["display_name"].lower())
    return bool(q) and all(t in words or (len(t) >= 3 and any(w.startswith(t) for w in words)) for t in q)


async def get_payer_summary(ctx: ToolContext, payer_id: str, **kw) -> dict:
    p = await _payer(ctx, payer_id)
    d1, d2, start, end = _window(kw["from"], kw["to"])
    w = await fetch_one(
        ctx.conn,
        """
        select count(*)::int as n, coalesce(sum(amount_paise), 0)::bigint as total,
               (percentile_cont(0.5) within group (order by amount_paise))::bigint as median,
               min(amount_paise) as min_amt, max(amount_paise) as max_amt,
               mode() within group (order by amount_paise) as most_common,
               (percentile_cont(0.5) within group (order by
                  extract(hour from paid_at at time zone 'Asia/Kolkata') * 60
                  + extract(minute from paid_at at time zone 'Asia/Kolkata')))::int as typical_minute,
               min(paid_at) as first_at, max(paid_at) as last_at
          from transactions where payer_id = %s and paid_at >= %s and paid_at < %s
        """,
        (p["id"], start, end),
    )
    profile = await ledger.payer_profile(ctx.conn, ctx.merchant_id, p["id"], txn_limit=0)
    share = None
    if w["n"]:
        same = await fetch_one(ctx.conn, "select count(*)::int as c from transactions where payer_id = %s "
                               "and paid_at >= %s and paid_at < %s and amount_paise = %s",
                               (p["id"], start, end, w["most_common"]))
        share = round(100 * same["c"] / w["n"])
    today = await app_today(ctx.conn)
    last_local = w["last_at"].astimezone(IST).date() if w["last_at"] else None
    data = {
        "payer_id": str(p["id"]), "name": p["display_name"], "vpa": profile["vpa"],
        "window": {"from": d1.isoformat(), "to": d2.isoformat()},
        "payments": w["n"], "total_rupees": _rs(w["total"]),
        "usual_amount_rupees": _rs(w["median"]), "most_common_amount_rupees": _rs(w["most_common"]),
        "percent_at_most_common_amount": share,
        "min_rupees": _rs(w["min_amt"]), "max_rupees": _rs(w["max_amt"]),
        "usual_time": ledger.clock_words(w["typical_minute"]),
        "first_payment": w["first_at"].astimezone(IST).strftime("%Y-%m-%d %H:%M") if w["first_at"] else None,
        "last_payment": w["last_at"].astimezone(IST).strftime("%Y-%m-%d %H:%M") if w["last_at"] else None,
        "last_payment_was": ("today" if last_local == today else "yesterday" if last_local == today - timedelta(days=1)
                             else f"{(today - last_local).days} days ago") if last_local else None,
        "lifetime": {"payments": profile["txn_count"], "total_rupees": _rs(profile["total_paise"]),
                     "habit": profile["habit"], "badges": profile["badges"]},
        "open_invoices": [{"invoice_id": str(i["id"]), "number": i["number"], "amount_rupees": _rs(i["amount_paise"]),
                           "status": i["status"], "due_date": i["due_date"].isoformat()}
                          for i in profile["invoices"] if i["status"] in ("sent", "overdue")],
        "recurring": bool(profile["recurring_schedule"]),
    }
    return data


async def list_transactions(ctx: ToolContext, payer_id: str | None, limit: int, **kw) -> dict:
    pid = (await _payer(ctx, payer_id))["id"] if payer_id else None
    d1, d2, start, end = _window(kw["from"], kw["to"])
    limit = max(1, min(200, int(limit)))
    rows = await ledger.list_transactions(ctx.conn, ctx.merchant_id, pid, start, end, limit)
    totals = await ledger.count_transactions(ctx.conn, ctx.merchant_id, pid, start, end)
    return {
        "window": {"from": d1.isoformat(), "to": d2.isoformat()},
        "payments": totals["txn_count"], "total_rupees": _rs(totals["total_paise"]),
        "rows_returned": len(rows),
        "note": (f"Only the first 20 rows are repeated here. The merchant's table lists {len(rows)} of the "
                 f"{totals['txn_count']} payments" + (" (with a Show all button)." if len(rows) >= totals["txn_count"]
                                                      else "; ask for a higher limit to list more.")
                 ) if len(rows) > 20 else None,
        "rows": [{"date": r["paid_at"].astimezone(IST).strftime("%Y-%m-%d %H:%M"),
                  "customer": r["payer_name"] or "Walk-in", "amount_rupees": _rs(r["amount_paise"]),
                  "description": r["description"]} for r in rows[:20]],
        "_full_rows": rows,
    }


async def get_sales_summary(ctx: ToolContext, grain: str, **kw) -> dict:
    d1, d2, start, end = _window(kw["from"], kw["to"])
    days = (d2 - d1).days + 1
    # A month or less reads best day by day. Longer ranges start with a partial week/month so the chart
    # covers exactly the window the totals are for; a sliver of one (31 Jul in a 3-month chart) would read
    # as a near-zero point, so the next bucket is folded into it and the point is labelled by that bucket.
    if days <= 31:
        grain = "day"
    points = await ledger.trend(ctx.conn, ctx.merchant_id, d2, days, grain, start=d1)
    if grain != "day" and len(points) > 1 and (points[1]["bucket"] - points[0]["bucket"]).days < (
            4 if grain == "week" else 15):  # under half a period
        first, second = points[0], points[1]
        points = [{"bucket": first["bucket"], "label_bucket": second["bucket"],
                   "total_paise": first["total_paise"] + second["total_paise"],
                   "txn_count": first["txn_count"] + second["txn_count"]}] + points[2:]
    totals = await ledger.count_transactions(ctx.conn, ctx.merchant_id, None, start, end)
    best = await fetch_one(
        ctx.conn,
        "select day, total_paise from daily_totals where merchant_id = %s and day between %s and %s "
        "order by total_paise desc limit 1",
        (ctx.merchant_id, d1, d2),
    )
    peak = await fetch_one(
        ctx.conn,
        """
        select extract(hour from paid_at at time zone 'Asia/Kolkata')::int as hour, sum(amount_paise)::bigint as total
          from transactions where merchant_id = %s and paid_at >= %s and paid_at < %s
         group by 1 order by 2 desc limit 1
        """,
        (ctx.merchant_id, start, end),
    )
    return {
        "window": {"from": d1.isoformat(), "to": d2.isoformat(), "days": days}, "grain": grain,
        "total_rupees": _rs(totals["total_paise"]), "payments": totals["txn_count"],
        "average_per_day_rupees": _rs(round(totals["total_paise"] / days / 100) * 100) if days else None,
        "best_day": {"date": best["day"].isoformat(), "total_rupees": _rs(best["total_paise"])} if best else None,
        "peak_hour": ledger.clock_words(peak["hour"] * 60) if peak else None,
        "series": [{"bucket": p["bucket"].isoformat(), "total_rupees": _rs(p["total_paise"]),
                    "payments": p["txn_count"]} for p in points],
        "_points": points,
    }


async def list_dues(ctx: ToolContext, status: str) -> dict:
    where = {"open": "('sent','overdue')", "overdue": "('overdue')", "paid": "('paid')",
             "all": "('draft','sent','overdue','paid','cancelled')"}[status]
    rows = await fetch_all(
        ctx.conn,
        f"""
        select i.id, i.number, p.display_name as customer, i.amount_paise, i.due_date, i.status, i.email_to,
               (app_today() - i.due_date) as days_overdue, i.paid_at
          from invoices i join payers p on p.id = i.payer_id
         where i.merchant_id = %s and i.status in {where}
         order by i.due_date limit 50
        """,
        (ctx.merchant_id,),
    )
    return {
        "status": status, "count": len(rows), "total_rupees": _rs(sum(r["amount_paise"] for r in rows)),
        "invoices": [{"invoice_id": str(r["id"]), "number": r["number"], "customer": r["customer"],
                      "amount_rupees": _rs(r["amount_paise"]), "due_date": r["due_date"].isoformat(),
                      "status": r["status"], "days_overdue": max(0, r["days_overdue"]),
                      "has_email": bool(r["email_to"])} for r in rows],
    }


# ---------------------------------------------------------------------------
# Write tools → pending actions
# ---------------------------------------------------------------------------
async def _pending(ctx: ToolContext, tool: str, args: dict, summary: dict) -> dict:
    token = "pa_" + secrets.token_urlsafe(12)
    expires = await app_now(ctx.conn) + PENDING_TTL
    await ctx.conn.execute(
        "insert into pending_actions (token, merchant_id, tool, args, summary, expires_at) values (%s, %s, %s, %s, %s, %s)",
        (token, ctx.merchant_id, tool, Jsonb(args), Jsonb(summary), expires),
    )
    action = {"token": token, "tool": tool, "summary": summary, "expires_at": expires.isoformat()}
    ctx.pending.append(action)
    return {"pending_action": True, "requires_merchant_confirmation": True, "summary": summary,
            "note": "Nothing has happened yet. Ask the merchant to tap Confirm.", "_token": token}


async def create_invoice(ctx: ToolContext, payer_id: str, amount: float, due_date: str, items: list[str],
                         email: str | None) -> dict:
    p = await _payer(ctx, payer_id)
    paise = rupees_to_paise(amount)
    if paise < 100:
        raise ToolError("amount must be at least 1 rupee")
    due = _date(due_date, "due_date")
    today = await app_today(ctx.conn)
    if due < today:
        raise ToolError("due_date is in the past")
    names = [i.strip() for i in items if i.strip()] or ["Payment"]
    line_items = [{"name": names[0][:80], "qty": 1, "unit_price_paise": paise}] if len(names) == 1 else [
        {"name": ", ".join(names)[:80], "qty": 1, "unit_price_paise": paise}]
    to = (email or p["email"] or "").strip().lower() or None
    summary = {"title": f"Invoice for {p['display_name']}", "customer": p["display_name"],
               "amount": format_inr(paise), "amount_rupees": _rs(paise), "due_date": due.isoformat(),
               "items": [li["name"] for li in line_items],
               "email": to, "delivery": f"Email to {to} with Paytm link and QR" if to else "Paytm link and QR (no email)"}
    return await _pending(ctx, "create_invoice", {"payer_id": str(p["id"]), "amount_paise": paise,
                                                  "due_date": due.isoformat(), "line_items": line_items,
                                                  "email": to}, summary)


async def send_reminder(ctx: ToolContext, invoice_id: str) -> dict:
    try:
        iid = UUID(invoice_id)
    except ValueError as e:
        raise ToolError("invoice_id must come from list_dues or get_payer_summary") from e
    inv = await fetch_one(
        ctx.conn,
        "select i.*, p.display_name, p.email as payer_email from invoices i join payers p on p.id = i.payer_id "
        "where i.id = %s and i.merchant_id = %s",
        (iid, ctx.merchant_id),
    )
    if inv is None:
        raise ToolError("no such invoice")
    if inv["status"] not in ("sent", "overdue"):
        raise ToolError(f"invoice is {inv['status']}; only unpaid invoices can be reminded")
    to = inv["email_to"] or inv["payer_email"]
    if not to:
        raise ToolError("this customer has no email address; the merchant must add one first")
    summary = {"title": f"Reminder for {inv['number']}", "customer": inv["display_name"],
               "amount": format_inr(inv["amount_paise"]), "amount_rupees": _rs(inv["amount_paise"]),
               "invoice": inv["number"], "email": to, "status": inv["status"]}
    return await _pending(ctx, "send_reminder", {"invoice_id": str(iid)}, summary)


async def set_recurring(ctx: ToolContext, payer_id: str, cadence: str, amount: float, email: str | None) -> dict:
    p = await _payer(ctx, payer_id)
    paise = rupees_to_paise(amount)
    if paise < 100:
        raise ToolError("amount must be at least 1 rupee")
    to = (email or p["email"] or "").strip().lower() or None
    summary = {"title": f"Mark {p['display_name']} as recurring", "customer": p["display_name"],
               "amount": format_inr(paise), "amount_rupees": _rs(paise), "cadence": cadence, "email": to,
               "delivery": (f"Invoices and overdue reminders emailed to {to}" if to
                            else "Tracking only: dues and missed payments, no emails")}
    return await _pending(ctx, "set_recurring", {"payer_id": str(p["id"]), "cadence": cadence,
                                                 "amount_paise": paise, "email": to}, summary)


IMPLS = {
    "find_payer": find_payer, "get_payer_summary": get_payer_summary, "list_transactions": list_transactions,
    "get_sales_summary": get_sales_summary, "list_dues": list_dues, "create_invoice": create_invoice,
    "send_reminder": send_reminder, "set_recurring": set_recurring,
}


async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[dict, bool]:
    """Execute a tool. Returns (model-facing result, is_error)."""
    if name not in IMPLS:
        return {"error": f"unknown tool {name}"}, True
    try:
        data = await IMPLS[name](ctx, **args)
    except ToolError as e:
        ctx.calls.append({"tool": name, "input": args, "error": str(e)})
        return {"error": str(e)}, True
    except TypeError as e:
        ctx.calls.append({"tool": name, "input": args, "error": f"bad arguments: {e}"})
        return {"error": f"bad arguments: {e}"}, True
    rid = ctx.register(name, data)
    visible = {k: v for k, v in data.items() if not k.startswith("_")}
    ctx.calls.append({"tool": name, "input": args, "result_id": rid})
    return {"result_id": rid, **visible}, False
