"""Paytm S2S webhooks: form key-value, checksum-verified, idempotent. No JWT."""

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..auth import Conn
from ..config import get_settings
from ..services.descriptions import describe_transaction_later
from ..services.payments import record_notification
from ..services.paytm.checksum import verify_form

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/paytm")


async def _handle(request: Request, conn, route: str, background: BackgroundTasks) -> dict:
    form = await request.form()
    fields = {k: str(v) for k, v in form.items()}
    if not verify_form(fields, get_settings().paytm_key):
        log.warning("rejected %s webhook with bad checksum: ORDERID=%s TXNID=%s", route,
                    fields.get("ORDERID"), fields.get("TXNID"))
        raise HTTPException(400, "invalid checksum")
    outcome = await record_notification(conn, fields, route)
    if outcome.get("result") == "paid" and outcome.get("transaction_id"):
        background.add_task(describe_transaction_later, outcome["transaction_id"])  # AI label for the ledger
    return {"ok": True, **{k: v for k, v in outcome.items() if k in ("result", "txn_id")}}


@router.post("/link")
async def link_webhook(request: Request, conn: Conn, background: BackgroundTasks):
    return await _handle(request, conn, "link", background)


@router.post("/payment")
async def payment_webhook(request: Request, conn: Conn, background: BackgroundTasks):
    return await _handle(request, conn, "payment", background)
