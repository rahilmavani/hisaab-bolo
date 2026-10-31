"""Sarvam voice agent → us: the outcome of a reminder call, and the agent's mid-call tool. No JWT;
Sarvam doesn't sign its requests, so the URLs we give it carry a shared secret (?token=)."""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request

from ..auth import Conn
from ..config import get_settings
from ..services.calls import record_call_outcome, send_link_during_call

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/sarvam")


async def _verified_json(request: Request, token: str) -> dict:
    expected = get_settings().call_webhook_secret
    if not expected or not secrets.compare_digest(token, expected):
        log.warning("rejected sarvam request to %s with a bad token", request.url.path)
        raise HTTPException(401, "bad token")
    try:
        payload = await request.json()
    except ValueError as e:
        raise HTTPException(400, "expected a JSON body") from e
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected a JSON object")
    return payload


@router.post("/call")
async def call_webhook(request: Request, conn: Conn, token: str = ""):
    payload = await _verified_json(request, token)
    return {"ok": True, **await record_call_outcome(conn, payload)}


@router.post("/tool/send-link")
async def send_link_tool(request: Request, conn: Conn, token: str = ""):
    """The agent's `send_payment_link` tool. Body: {"call_token": "@call_token"}. Always 200 with a
    `message` the agent can say, so a refusal never breaks the conversation."""
    payload = await _verified_json(request, token)
    return await send_link_during_call(conn, str(payload.get("call_token") or "").strip())
