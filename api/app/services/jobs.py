"""/jobs/tick: the 5-minute background sweep (Railway cron, or in-process when RUN_SCHEDULER=true)."""

import asyncio
import logging

from psycopg import AsyncConnection

from ..db import app_now, connection, fetch_all, fetch_one
from . import ledger, recurring
from .descriptions import describe_pending
from .payments import poll_open_invoices

log = logging.getLogger(__name__)
TICK_LOCK = 4_242_001
INTERVAL_SECONDS = 300


async def tick(conn: AsyncConnection) -> dict:
    got = await fetch_one(conn, "select pg_try_advisory_lock(%s) as ok", (TICK_LOCK,))
    if not got["ok"]:
        return {"skipped": "another tick is running"}
    try:
        summary: dict = {"now": await app_now(conn)}
        steps = [
            ("dues_created", recurring.create_due_invoices(conn)),
            ("due_emails", recurring.send_unsent_due_emails(conn)),
            ("overdue", recurring.mark_overdue_and_remind(conn)),
            ("paid_by_status_poll", poll_open_invoices(conn)),
            ("descriptions_written", describe_pending(conn)),
            ("alerts", alerts(conn)),
        ]
        for name, step in steps:
            try:
                summary[name] = await step
            except Exception as e:  # one failing step must not block the others
                log.exception("tick step %s failed", name)
                summary[name] = {"error": str(e)}
        return summary
    finally:
        await conn.execute("select pg_advisory_unlock(%s)", (TICK_LOCK,))


async def alerts(conn: AsyncConnection) -> list[dict]:
    """Lost regulars and slow days per merchant (computed on read; summarised here for the log)."""
    out = []
    now = await app_now(conn)
    for m in await fetch_all(conn, "select id, name from merchants"):
        lost = await ledger.lost_regulars(conn, m["id"])
        slow = await ledger.slow_day(conn, m["id"], now)
        if lost or slow["is_slow"]:
            out.append({"merchant": m["name"], "lost_regulars": [p["display_name"] for p in lost],
                        "slow_day_drop_pct": slow["drop_pct"] if slow["is_slow"] else None})
    return out


async def scheduler_loop() -> None:
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        try:
            async with connection() as conn:
                summary = await tick(conn)
            log.info("scheduled tick: %s", {k: v for k, v in summary.items() if v})
        except Exception:
            log.exception("scheduled tick failed")
