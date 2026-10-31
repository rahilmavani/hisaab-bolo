"""Tester round 10, M6 voice: /voice/ask and /voice/tts with Sarvam and Claude stubbed, plus the ffmpeg fallback.

Spec (docs/ARCHITECTURE.md) voice path: 1 record with MediaRecorder; 2 "If Sarvam rejects webm, convert to 16 kHz WAV
with ffmpeg"; 3 Sarvam STT; 6 number guard; 7 "Bulbul TTS. Text renders first, and the audio plays when ready."
API copy (agent.py): "Didn't catch that. Hold the mic and speak, or type your question.", "Recording is too long; keep
questions under 30 seconds", "Couldn't make the voice reply right now. Try again."
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.services import sarvam
from app.services.agent.runner import _months_back

from ..conftest import bearer
from .test_r9_m6_agent import FakeClaude, final, last_tool_result, tool_use


@pytest.fixture
def fake_claude(monkeypatch):
    def install(steps):
        fake = FakeClaude(steps)
        monkeypatch.setattr("app.services.agent.runner.client", lambda: fake)
        return fake
    return install


def stt(monkeypatch, transcript: str, lang="hi-IN"):
    async def transcribe(audio, ct):
        return {"transcript": transcript, "language_code": lang}
    monkeypatch.setattr("app.services.sarvam.transcribe", transcribe)


def moment1_steps(today, display, speech):
    return [
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use("get_payer_summary", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                         "from": str(_months_back(today, 4)), "to": str(today)}, 2)],
        lambda m, model: [final(display=display, speech=speech, components=[{"type": "payer_card", "result_id": "r2"}])],
    ]


async def voice(api, token, data=b"x" * 2000, ct="audio/webm"):
    return await api.post("/voice/ask", files={"audio": ("q.webm", data, ct)}, headers=bearer(token))


async def test_voice_moment1_in_standard_hindi_is_spoken(api, demo_token, monkeypatch, fake_claude, db):
    """Live, Saaras wrote "राम ने पिछले चार महीनों में कितना दिया?" for the spoken question (2 of 2 runs fell back with no
    speech). The plural "महीनों" is not a time word for the guard, so the "4" every correct answer repeats is rejected."""
    stt(monkeypatch, "राम ने पिछले चार महीनों में कितना दिया?")
    today = db.execute("select app_today() as d").fetchone()["d"]
    steps = moment1_steps(today, "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments.",
                          "राम ने पिछले 4 महीनों में ₹12,100 दिए, 121 payments।")
    fake_claude(steps + moment1_steps(today, "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments.",
                                      "राम ने पिछले 4 महीनों में ₹12,100 दिए, 121 payments।"))
    r = await voice(api, demo_token)
    assert r.status_code == 200, r.text
    out = r.json()
    assert not out["fallback"] and out["speech"], f"spoken moment 1 fell back: {out['display']!r}"


@pytest.mark.parametrize("transcript", ["॥", "…", "?!", " ।। "])
async def test_punctuation_only_transcripts_are_didnt_catch_that(api, demo_token, monkeypatch, fake_claude, transcript):
    """Round-9 fix re-check (more punctuation)."""
    stt(monkeypatch, transcript)
    fake = fake_claude([lambda m, model: [final(display="?", speech="")]] * 2)
    r = await voice(api, demo_token)
    assert r.status_code == 422 and "Didn't catch that" in r.json()["detail"]
    assert fake.calls == []


async def test_tts_failure_has_friendly_copy_not_sarvams_text(api, demo_token, monkeypatch, fake_claude, db):
    """Round-9 fix re-check: a Bulbul error is not shown raw."""
    today = db.execute("select app_today() as d").fetchone()["d"]
    fake_claude(moment1_steps(today, "Ram ne ₹12,100 diye, 121 payments.", "राम ने ₹12,100 दिए, 121 payments।"))
    r = await api.post("/ask", json={"text": "Ram ne pichhle 4 mahine mein kitna diya?"}, headers=bearer(demo_token))
    qid = r.json()["query_id"]

    async def broken(text, lang="hi-IN"):
        raise sarvam.SarvamError('text-to-speech 400: {"error":{"message":"Invalid speaker for model bulbul:v3"}}')
    monkeypatch.setattr("app.services.sarvam.speak", broken)
    r = await api.post("/voice/tts", json={"query_id": qid}, headers=bearer(demo_token))
    assert r.status_code == 502 and r.json()["detail"] == "Couldn't make the voice reply right now. Try again."


async def test_upload_over_the_cap_is_too_long(api, demo_token, monkeypatch, fake_claude):
    """Latest commit reads at most MAX_AUDIO_BYTES + 1: an oversized upload still gets the too-long copy."""
    called = []

    async def transcribe(audio, ct):
        called.append(len(audio))
        return {"transcript": "x", "language_code": "hi-IN"}
    monkeypatch.setattr("app.services.sarvam.transcribe", transcribe)
    r = await voice(api, demo_token, data=b"\0" * (5 * 1024 * 1024 + 10))
    assert r.status_code == 413 and "too long" in r.json()["detail"] and called == []


ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _encode(fmt_args: list[str]) -> bytes:
    """One second of a 440 Hz tone, encoded the way a browser's MediaRecorder might."""
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                           "sine=frequency=440:duration=1", *fmt_args, "pipe:1"], capture_output=True, check=True).stdout


@ffmpeg
@pytest.mark.parametrize("name,args", [
    ("webm/opus (Chrome)", ["-c:a", "libopus", "-f", "webm"]),
    ("ogg/opus (Firefox)", ["-c:a", "libopus", "-f", "ogg"]),
    ("fragmented mp4/aac (Safari)", ["-c:a", "aac", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4"]),
    ("wav", ["-f", "wav"]),
])
async def test_ffmpeg_fallback_still_converts_browser_recordings(name, args):
    """Voice path 2: if Sarvam rejects the recording, it is converted to 16 kHz WAV. The latest commit whitelists
    containers; each format a browser records in must still convert."""
    try:
        audio = _encode(args)
    except subprocess.CalledProcessError:
        pytest.skip(f"this ffmpeg can't encode {name}")
    wav = await sarvam.to_wav_16k(audio)
    assert wav[:4] == b"RIFF" and len(wav) > 16000, name


@ffmpeg
async def test_ffmpeg_fallback_on_garbage_is_unreadable_not_upstream(api, demo_token, monkeypatch):
    """Sarvam rejects the bytes, ffmpeg can't read them either: the merchant gets the 'audio couldn't be read' copy."""
    async def rejecting(audio, filename, content_type):
        raise sarvam.SarvamError("speech-to-text 400: unsupported file")
    monkeypatch.setattr("app.services.sarvam._stt", rejecting)
    r = await voice(api, demo_token, data=b"not audio at all" * 200)
    assert r.status_code == 502 and "couldn't be read" in r.json()["detail"], r.text
