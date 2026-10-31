import json
import logging
import re
import time
from collections import OrderedDict
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from ..auth import Conn, CurrentMerchant
from ..db import fetch_one
from ..services import sarvam
from ..services.agent.actions import ActionError, confirm
from ..services.agent.runner import ask

log = logging.getLogger(__name__)
router = APIRouter()
MAX_AUDIO_BYTES = 5 * 1024 * 1024
_tts_cache: "OrderedDict[str, bytes]" = OrderedDict()  # keyed by merchant and answer
TOO_LONG = "Recording is too long; keep questions under 30 seconds"
DIDNT_CATCH = "Didn't catch that. Hold the mic and speak, or type your question."
STT_COPY = {  # SarvamError.kind -> (status, what the merchant sees)
    "empty": (422, DIDNT_CATCH),
    "too_long": (413, TOO_LONG),
    "unreadable": (502, "Couldn't understand the recording: the audio couldn't be read. Try again, or type your question."),
    "upstream": (502, "Couldn't understand the recording: speech-to-text isn't responding. Try again, or type your question."),
}


class HistoryItem(BaseModel):
    """Earlier turns are context only: long ones (a chip re-ask, a long voice transcript) are shortened, not refused."""
    question: str = Field(max_length=5000)
    answer: str = Field(max_length=10000)

    @field_validator("question")
    @classmethod
    def _short_question(cls, v: str) -> str:
        return v[:500]

    @field_validator("answer")
    @classmethod
    def _short_answer(cls, v: str) -> str:
        return v[:2000]


class AskBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)

    @field_validator("text", mode="before")
    @classmethod
    def _stripped(cls, v):
        return v.strip() if isinstance(v, str) else v  # "   " is empty, not a question for the model

    @field_validator("text")
    @classmethod
    def _has_words(cls, v: str) -> str:
        if not re.search(r"\w", v):  # "?", "।", "..." are not questions either
            raise ValueError("Type a question about your shop")
        return v
    payer_hint: UUID | None = None
    history: list[HistoryItem] = Field(default_factory=list, max_length=6)


@router.post("/ask")
async def ask_text(body: AskBody, merchant: CurrentMerchant, conn: Conn):
    return await ask(conn, merchant.id, merchant.name, body.text.strip(),
                     history=[h.model_dump() for h in body.history],
                     payer_hint=str(body.payer_hint) if body.payer_hint else None, source="text")


@router.post("/voice/ask")
async def ask_voice(
    merchant: CurrentMerchant,
    conn: Conn,
    audio: UploadFile = File(...),
    payer_hint: UUID | None = Form(None),
    history: str | None = Form(None),
):
    data = await audio.read(MAX_AUDIO_BYTES + 1)  # never hold more than the cap in memory
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, TOO_LONG)
    if not data:
        raise HTTPException(422, DIDNT_CATCH)
    started = time.monotonic()
    try:
        stt = await sarvam.transcribe(data, audio.content_type or "audio/webm")
    except sarvam.SarvamError as e:
        log.warning("STT failed (%s): %s", e.kind, e)
        status, copy = STT_COPY.get(e.kind, STT_COPY["upstream"])
        raise HTTPException(status, copy) from e
    stt_ms = round((time.monotonic() - started) * 1000)
    if not re.search(r"\w", stt["transcript"]):  # nothing, or only punctuation ("।", "...")
        raise HTTPException(422, DIDNT_CATCH)
    hist = []
    if history:
        try:
            hist = [HistoryItem(**h).model_dump() for h in json.loads(history)][-3:]
        except (ValueError, TypeError):
            hist = []
    out = await ask(conn, merchant.id, merchant.name, stt["transcript"], history=hist,
                    payer_hint=str(payer_hint) if payer_hint else None, source="voice",
                    heard_lang=stt["language_code"])
    return {**out, "transcript": stt["transcript"], "stt_language": stt["language_code"], "stt_ms": stt_ms}


class TtsBody(BaseModel):
    query_id: UUID


@router.post("/voice/tts")
async def tts(body: TtsBody, merchant: CurrentMerchant, conn: Conn):
    """Speak an answer the agent already gave (only grounded speech is ever synthesised)."""
    key = f"{merchant.id}:{body.query_id}"  # the cache never serves one merchant's answer to another
    if key not in _tts_cache:
        row = await fetch_one(conn, "select answer from agent_queries where id = %s and merchant_id = %s",
                              (body.query_id, merchant.id))
        if row is None:
            raise HTTPException(404, "Unknown answer")
        speech = (row["answer"] or {}).get("speech")
        if not speech:
            raise HTTPException(404, "This answer has no speech")
        try:
            _tts_cache[key] = await sarvam.speak(speech, (row["answer"] or {}).get("lang", "hi-IN"))
        except sarvam.SarvamError as e:
            log.warning("TTS failed: %s", e)
            raise HTTPException(502, "Couldn't make the voice reply right now. Try again.") from e
        while len(_tts_cache) > 64:
            _tts_cache.popitem(last=False)
    return Response(content=_tts_cache[key], media_type="audio/mpeg")


@router.post("/actions/{token}/confirm")
async def confirm_action(token: str, merchant: CurrentMerchant, conn: Conn):
    try:
        return await confirm(conn, merchant.id, token)
    except ActionError as e:
        raise HTTPException(e.status, str(e)) from e
