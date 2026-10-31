"""Sarvam AI: Saaras speech-to-text (codemix) and Bulbul text-to-speech."""

import asyncio
import base64
import logging
import shutil

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)
BASE = "https://api.sarvam.ai"
STT_MODEL = "saaras:v4"
TTS_MODEL = "bulbul:v3"
TTS_SPEAKER = {"hi-IN": "priya", "en-IN": "priya"}


class SarvamError(Exception):
    """kind: "empty" or "too_long" (the merchant's recording), "unreadable" (audio we couldn't decode),
    "upstream" (Sarvam or our setup). The message is for logs; the API maps kind to friendly copy."""

    def __init__(self, message: str, kind: str = "upstream"):
        super().__init__(message)
        # Sarvam's own wording for a recording over its 30-second limit
        self.kind = "too_long" if "duration exceeds" in message.lower() else kind


def _headers() -> dict:
    key = get_settings().sarvam_api_key
    if not key:
        raise SarvamError("SARVAM_API_KEY is not set")
    return {"api-subscription-key": key}


async def to_wav_16k(audio: bytes) -> bytes:
    """ffmpeg fallback when Sarvam rejects the browser's recording format."""
    if not shutil.which("ffmpeg"):
        raise SarvamError("ffmpeg is not installed")
    # Untrusted upload: only the piped bytes may be read (no file/http/hls follow-ups from a crafted
    # playlist) and only the containers browsers record in; a stuck decode is killed.
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "pipe", "-format_whitelist", "matroska,webm,ogg,mov,mp4,wav",
        "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(audio), timeout=20)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise SarvamError("ffmpeg timed out", "unreadable") from None
    if proc.returncode != 0:
        raise SarvamError(f"ffmpeg failed: {err.decode()[:200]}", "unreadable")
    return out


async def _stt(audio: bytes, filename: str, content_type: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{BASE}/speech-to-text", headers=_headers(),
            data={"model": STT_MODEL, "mode": "codemix", "language_code": "unknown"},
            files={"file": (filename, audio, content_type)},
        )
    if r.status_code >= 400:
        raise SarvamError(f"speech-to-text {r.status_code}: {r.text[:300]}")
    return r.json()


async def transcribe(audio: bytes, content_type: str = "audio/webm") -> dict:
    """Returns {"transcript", "language_code"}. Retries as 16 kHz WAV if the original format is rejected."""
    if not audio:
        raise SarvamError("empty recording", "empty")
    ext = "webm" if "webm" in content_type else "ogg" if "ogg" in content_type else "mp4" if "mp4" in content_type else "wav"
    try:
        data = await _stt(audio, f"question.{ext}", content_type.split(";")[0])
    except SarvamError as e:
        if e.kind == "too_long":
            raise
        log.info("STT rejected %s (%s); retrying as 16 kHz WAV", content_type, e)
        data = await _stt(await to_wav_16k(audio), "question.wav", "audio/wav")
    return {"transcript": (data.get("transcript") or "").strip(), "language_code": data.get("language_code")}


async def speak(text: str, lang: str = "hi-IN") -> bytes:
    """Bulbul TTS → MP3 bytes."""
    if not text.strip():
        raise SarvamError("nothing to say")
    body = {
        "text": text[:2500],
        "model": TTS_MODEL,
        "speaker": TTS_SPEAKER.get(lang, "priya"),
        "target_language_code": lang,
        "output_audio_codec": "mp3",
        "pace": 1.05,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{BASE}/text-to-speech", headers=_headers(), json=body)
    if r.status_code >= 400:
        raise SarvamError(f"text-to-speech {r.status_code}: {r.text[:300]}")
    audios = r.json().get("audios") or []
    if not audios:
        raise SarvamError("text-to-speech returned no audio")
    return base64.b64decode("".join(audios))
