from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import AfterValidator, BaseModel, EmailStr, Field, StringConstraints

from ..auth import Conn, CurrentMerchant
from ..config import get_settings
from ..db import app_now, fetch_one
from ..services import ledger
from ..services.payers import PayerError, merge_payers, update_payer
from ..validation import SafeStr, check_date, check_query_text, no_nul

router = APIRouter()


@router.get("/me")
async def me(merchant: CurrentMerchant, conn: Conn):
    clock = await fetch_one(conn, "select override_now, set_at from app_clock where id = 1")
    now = await app_now(conn)
    s = get_settings()
    return {
        "merchant": {"id": merchant.id, "name": merchant.name},
        "now": now,
        "clock_overridden": bool(clock and clock["override_now"]),
        "paytm_mode": s.paytm_mode,
        "demo_tools": s.mock_mode,
    }


@router.get("/dashboard/summary")
async def dashboard_summary(merchant: CurrentMerchant, conn: Conn, date: date | None = None):
    return await ledger.dashboard_summary(conn, merchant.id, check_date(date))


@router.get("/dashboard/trend")
async def dashboard_trend(
    merchant: CurrentMerchant,
    conn: Conn,
    days: int = Query(30, ge=1, le=366),
    grain: Literal["day", "week", "month"] = "day",
    end: date | None = None,
):
    check_date(end, "end")
    if end is None:
        end = (await app_now(conn)).astimezone(ledger.IST).date()
    return {"days": days, "grain": grain, "end": end,
            "points": await ledger.trend(conn, merchant.id, end, days, grain)}


@router.get("/payers")
async def list_payers(merchant: CurrentMerchant, conn: Conn, q: str = "", limit: int = Query(50, ge=1, le=200)):
    return {"payers": await ledger.search_payers(conn, merchant.id, check_query_text(q), limit)}


@router.get("/payers/{payer_id}")
async def get_payer(payer_id: UUID, merchant: CurrentMerchant, conn: Conn):
    profile = await ledger.payer_profile(conn, merchant.id, payer_id)
    if profile is None:
        raise HTTPException(404, "payer not found")
    return profile


class PayerPatch(BaseModel):
    display_name: SafeStr | None = Field(None, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=20, pattern=r"^[+\d\s().-]*$")
    gstin: str | None = Field(None, pattern=r"^[0-9]{2}[A-Z0-9]{13}$")
    notes: SafeStr | None = Field(None, max_length=2000)


def _raise(e: PayerError):
    raise HTTPException(e.status, str(e))


@router.patch("/payers/{payer_id}")
async def patch_payer(payer_id: UUID, body: PayerPatch, merchant: CurrentMerchant, conn: Conn):
    try:
        await update_payer(conn, merchant.id, payer_id, body.model_dump(exclude_unset=True))
    except PayerError as e:
        _raise(e)
    return await ledger.payer_profile(conn, merchant.id, payer_id)


class MergeBody(BaseModel):
    other_payer_id: UUID


@router.post("/payers/{payer_id}/merge")
async def merge(payer_id: UUID, body: MergeBody, merchant: CurrentMerchant, conn: Conn):
    """Merge `other_payer_id` into this payer (this payer survives)."""
    try:
        await merge_payers(conn, merchant.id, payer_id, body.other_payer_id)
    except PayerError as e:
        _raise(e)
    return await ledger.payer_profile(conn, merchant.id, payer_id)


@router.get("/transactions")
async def transactions(
    merchant: CurrentMerchant,
    conn: Conn,
    payer_id: UUID | None = None,
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    # Times without an offset are IST, like everything the merchant sees.
    start = start.replace(tzinfo=ledger.IST) if start and start.tzinfo is None else start
    end = end.replace(tzinfo=ledger.IST) if end and end.tzinfo is None else end
    rows = await ledger.list_transactions(conn, merchant.id, payer_id, start, end, limit, offset)
    totals = await ledger.count_transactions(conn, merchant.id, payer_id, start, end)
    return {"transactions": rows, **totals}


class TransactionPatch(BaseModel):
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
                           AfterValidator(no_nul)] | None = None
    category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
                        AfterValidator(no_nul)] | None = None


@router.patch("/transactions/{txn_id}")
async def patch_transaction(txn_id: UUID, body: TransactionPatch, merchant: CurrentMerchant, conn: Conn):
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise HTTPException(422, "nothing to update")
    sets = ", ".join(f"{k} = %({k})s" for k in changes)
    row = await fetch_one(
        conn,
        f"update transactions set {sets}, description_by = 'merchant' "
        "where id = %(id)s and merchant_id = %(m)s returning id",
        {**changes, "id": txn_id, "m": merchant.id},
    )
    if row is None:
        raise HTTPException(404, "transaction not found")
    return await ledger.get_transaction(conn, merchant.id, txn_id)
