from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import Conn, CurrentMerchant, require_cron_secret
from ..config import get_settings
from ..db import app_now, fetch_one
from ..money import IST
from ..services.jobs import tick
from ..services.recurring import reopen_not_yet_overdue

router = APIRouter()


@router.post("/jobs/tick", dependencies=[Depends(require_cron_secret)])
async def run_tick(conn: Conn):
    return await tick(conn)


class ClockBody(BaseModel):
    days: float | None = Field(None, ge=-365, le=365)
    hours: float | None = Field(None, ge=-24 * 365, le=24 * 365)
    set_to: datetime | None = None
    reset: bool = False
    run_jobs: bool = True


def _require_demo_tools():
    if not get_settings().mock_mode:
        raise HTTPException(404, "Not found")


async def _clock(conn) -> dict:
    row = await fetch_one(conn, "select override_now is distinct from base_override_now as moved from app_clock where id = 1")
    return {"now": await app_now(conn), "overridden": bool(row and row["moved"])}


@router.get("/admin/clock")
async def get_clock(merchant: CurrentMerchant, conn: Conn):
    _require_demo_tools()
    return await _clock(conn)


@router.post("/admin/clock")
async def set_clock(body: ClockBody, merchant: CurrentMerchant, conn: Conn):
    """Demo time jump: every job and query reads app_now(). Runs the jobs at the new time."""
    _require_demo_tools()
    if body.reset:  # back to where the seed put the clock (real time when it didn't pin it)
        await conn.execute("update app_clock set override_now = base_override_now, set_at = base_set_at where id = 1")
    else:
        if body.set_to is None and body.days is None and body.hours is None:
            raise HTTPException(422, "Give days, hours, set_to or reset")
        set_to = body.set_to.replace(tzinfo=IST) if body.set_to and body.set_to.tzinfo is None else body.set_to
        target = set_to or (await app_now(conn)) + timedelta(days=body.days or 0, hours=body.hours or 0)
        await conn.execute("update app_clock set override_now = %s, set_at = now() where id = 1", (target,))
    out = await _clock(conn)
    if reopened := await reopen_not_yet_overdue(conn):  # the clock went back
        out["reopened"] = reopened
    if body.run_jobs:
        out["jobs"] = await tick(conn)
    return out
