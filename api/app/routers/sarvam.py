"""Sarvam voice-agent webhook: the outcome of a reminder call. No JWT; Sarvam doesn't sign its
webhooks, so the URL we hand it carries a shared secret (?token=)."""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request

from ..auth import Conn
from ..config import get_settings
from ..services.calls import record_call_outcome

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/sarvam")


@router.post("/call")
async def call_webhook(request: Request, conn: Conn, token: str = ""):
    expected = get_settings().call_webhook_secret
    if not expected or not secrets.compare_digest(token, expected):
        log.warning("rejected sarvam call webhook with a bad token")
        raise HTTPException(401, "bad token")
    try:
        payload = await request.json()
    except ValueError as e:
        raise HTTPException(400, "expected a JSON body") from e
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected a JSON object")
    return {"ok": True, **await record_call_outcome(conn, payload)}
