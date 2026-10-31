"""Tester round 8, M6 voice: /voice/ask and /voice/tts with Sarvam and Claude stubbed.

Spec (docs/ARCHITECTURE.md): voice path 1-3 (MediaRecorder -> POST /voice/ask -> Sarvam STT, ffmpeg 16 kHz WAV
fallback), 7 ("Bulbul TTS. Text renders first, and the audio plays when ready"), API surface (POST /voice/ask,
POST /voice/tts), "Supabase JWT on all routes", Risks ("Wi-Fi or mic failure: the text fallback").
UI/API copy: "Recording is too long; keep questions under 30 seconds" (agent.py), "Didn't catch that. Hold the mic
and speak, or type your question." (agent.py), use-recorder.ts MAX_MS = 30_000 "Sarvam's real-time limit".
Live check this round: Sarvam rejects anything over 30.0 s ("Audio duration exceeds the maximum limit of 30
seconds"), including a 30.1 s webm.
"""

from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace

import pytest

from app.services import sarvam

from ..conftest import bearer
from .test_r8_m6_agent import FakeClaude, final, tool_use, last_tool_result


@pytest.fixture
def fake_claude(monkeypatch):
    def install(steps):
        fake = FakeClaude(steps)
        monkeypatch.setattr("app.services.agent.runner.client", lambda: fake)
        return fake
    return install


@pytest.fixture
def spoken(monkeypatch):
    calls = []

    async def speak(text, lang="hi-IN"):
        calls.append((text, lang))
        return b"ID3" + text.encode()
    monkeypatch.setattr("app.services.sarvam.speak", speak)
    return calls


def stt(monkeypatch, transcript="राम ने पिछले चार महीने में कितना दिया?", lang="hi-IN"):
    seen = []

    async def transcribe(audio, ct):
        seen.append((len(audio), ct))
        return {"transcript": transcript, "language_code": lang}
    monkeypatch.setattr("app.services.sarvam.transcribe", transcribe)
    return seen


def wav_seconds(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


def answer_steps(speech="राम ने ₹12,100 दिए।"):
    return [
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [final(display="Ram ne ₹12,100 diye.", speech=speech, lang="hi-IN",
                                components=[{"type": "chips", "result_id": "r1"}])],
    ]


# ---------------------------------------------------------------------------
# /voice/ask
# ---------------------------------------------------------------------------
async def test_voice_question_goes_through_the_same_agent_as_text(api, demo_token, fake_claude, monkeypatch):
    seen = stt(monkeypatch)
    fake = fake_claude(answer_steps())
    hist = json.dumps([{"question": "Aaj kitna hua?", "answer": "₹1,953."}])
    r = await api.post("/voice/ask", files={"audio": ("q.webm", b"\x1aE\xdf\xa3" + b"x" * 2000, "audio/webm;codecs=opus")},
                       data={"history": hist}, headers=bearer(demo_token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["transcript"].startswith("राम") and out["stt_language"] == "hi-IN" and out["stt_ms"] >= 0
    assert out["speech"] == "राम ने ₹12,100 दिए।" and out["lang"] == "hi-IN"
    assert seen == [(2004, "audio/webm;codecs=opus")]
    msgs = [m for m in fake.calls[0]["messages"] if isinstance(m["content"], str)]
    assert msgs[0]["content"] == "Aaj kitna hua?" and msgs[2]["content"].endswith("Voice transcript: " + out["transcript"])


async def test_voice_needs_auth(api):
    r = await api.post("/voice/ask", files={"audio": ("q.webm", b"x" * 100, "audio/webm")})
    assert r.status_code == 401
    assert (await api.post("/voice/tts", json={"query_id": "00000000-0000-0000-0000-000000000000"})).status_code == 401


async def test_silent_recording_gets_the_friendly_retry_message(api, demo_token, monkeypatch, fake_claude):
    stt(monkeypatch, transcript="")
    fake = fake_claude([])
    r = await api.post("/voice/ask", files={"audio": ("q.wav", wav_seconds(2), "audio/wav")}, headers=bearer(demo_token))
    assert r.status_code == 422 and r.json()["detail"].startswith("Didn't catch that")
    assert fake.calls == []


async def test_empty_upload_is_a_client_error_with_the_retry_message(api, demo_token):
    """A zero-byte recording (mic produced nothing) is the merchant's side, like silence: same 422 and copy,
    not a 502 "Couldn't understand the recording: empty recording"."""
    r = await api.post("/voice/ask", files={"audio": ("q.webm", b"", "audio/webm")}, headers=bearer(demo_token))
    assert r.status_code == 422, (r.status_code, r.text)


async def test_recording_over_5mb_is_413_with_the_30_second_copy(api, demo_token, monkeypatch):
    seen = stt(monkeypatch)
    r = await api.post("/voice/ask", files={"audio": ("q.wav", b"\x00" * (5 * 1024 * 1024 + 1), "audio/wav")},
                       headers=bearer(demo_token))
    assert r.status_code == 413 and "30 seconds" in r.json()["detail"] and seen == []


async def test_recording_over_30_seconds_gets_the_30_second_copy_not_sarvams_json(api, demo_token, monkeypatch):
    """A 35 s WAV is 1.1 MB, well under the 5 MB cap, so it reaches Sarvam, which refuses it; the merchant then sees
    'speech-to-text 400: {"error":{"message":"Audio duration exceeds ...","code":"invalid_request_error",...}}'.
    The API already has friendly copy for this case ("keep questions under 30 seconds")."""
    async def sarvam_refuses(audio, filename, content_type):
        raise sarvam.SarvamError('speech-to-text 400: {"error":{"message":"Audio duration exceeds the maximum limit '
                                 'of 30 seconds. Please use the batch API for longer audio files.","code":'
                                 '"invalid_request_error","request_id":"20261031_x"}}')
    monkeypatch.setattr("app.services.sarvam._stt", sarvam_refuses)
    r = await api.post("/voice/ask", files={"audio": ("q.wav", wav_seconds(35), "audio/wav")}, headers=bearer(demo_token))
    detail = r.json()["detail"]
    assert r.status_code in (413, 422), (r.status_code, detail)
    assert "{" not in detail and "invalid_request_error" not in detail, detail


async def test_stt_outage_is_502_with_a_readable_message(api, demo_token, monkeypatch):
    async def down(audio, ct):
        raise sarvam.SarvamError("speech-to-text 503: upstream unavailable")
    monkeypatch.setattr("app.services.sarvam.transcribe", down)
    r = await api.post("/voice/ask", files={"audio": ("q.webm", b"x" * 2000, "audio/webm")}, headers=bearer(demo_token))
    assert r.status_code == 502 and r.json()["detail"].startswith("Couldn't understand the recording")


async def test_unusable_audio_does_not_show_ffmpeg_internals(api, demo_token, monkeypatch):
    """When Sarvam rejects the format and the ffmpeg retry also fails, the chat shows the detail verbatim."""
    async def rejects(audio, filename, content_type):
        raise sarvam.SarvamError("speech-to-text 400: unsupported format")
    monkeypatch.setattr("app.services.sarvam._stt", rejects)
    r = await api.post("/voice/ask", files={"audio": ("q.webm", b"not audio at all" * 50, "audio/webm")},
                       headers=bearer(demo_token))
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "pipe:0" not in detail and "ffmpeg" not in detail, detail


async def test_webm_rejected_by_sarvam_is_retried_as_16k_wav(monkeypatch):
    calls = []

    async def fake_stt(audio, filename, content_type):
        calls.append((filename, content_type, audio[:4]))
        if content_type != "audio/wav":
            raise sarvam.SarvamError("speech-to-text 400: unsupported")
        return {"transcript": " राम ", "language_code": "hi-IN"}
    monkeypatch.setattr("app.services.sarvam._stt", fake_stt)
    webm = wav_seconds(1)  # any decodable audio; ffmpeg re-encodes it
    out = await sarvam.transcribe(webm, "audio/webm;codecs=opus")
    assert out == {"transcript": "राम", "language_code": "hi-IN"}
    assert calls[0][:2] == ("question.webm", "audio/webm") and calls[1][:3] == ("question.wav", "audio/wav", b"RIFF")


async def test_stt_asks_sarvam_for_codemix(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"transcript": "Ram ne kitna diya", "language_code": "hi-IN"}

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, data=None, files=None, json=None):
            sent.update(url=url, data=data, files=files)
            return Resp()
    monkeypatch.setattr("app.services.sarvam.httpx.AsyncClient", Client)
    monkeypatch.setattr("app.services.sarvam._headers", lambda: {"api-subscription-key": "x"})
    await sarvam.transcribe(b"abc", "audio/webm")
    assert sent["url"].endswith("/speech-to-text") and sent["data"]["mode"] == "codemix"
    assert sent["data"]["model"].startswith("saaras")


# ---------------------------------------------------------------------------
# /voice/tts
# ---------------------------------------------------------------------------
async def ask_once(api, token, fake_claude, speech="राम ने ₹12,100 दिए।") -> dict:
    fake_claude(answer_steps(speech))
    r = await api.post("/ask", json={"text": "Ram ne kitna diya?"}, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


async def test_tts_speaks_the_stored_grounded_speech_in_its_language(api, demo_token, fake_claude, spoken):
    out = await ask_once(api, demo_token, fake_claude)
    r = await api.post("/voice/tts", json={"query_id": out["query_id"]}, headers=bearer(demo_token))
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert spoken == [("राम ने ₹12,100 दिए।", "hi-IN")]


async def test_tts_for_an_answer_without_speech_is_404(api, demo_token, fake_claude, spoken):
    out = await ask_once(api, demo_token, fake_claude, speech="")
    r = await api.post("/voice/tts", json={"query_id": out["query_id"]}, headers=bearer(demo_token))
    assert r.status_code == 404 and spoken == []


async def test_tts_outage_is_502(api, demo_token, fake_claude, monkeypatch):
    out = await ask_once(api, demo_token, fake_claude)

    async def down(text, lang="hi-IN"):
        raise sarvam.SarvamError("text-to-speech 503: busy")
    monkeypatch.setattr("app.services.sarvam.speak", down)
    r = await api.post("/voice/tts", json={"query_id": out["query_id"]}, headers=bearer(demo_token))
    assert r.status_code == 502


async def test_tts_audio_is_merchant_scoped_even_after_it_is_cached(api, demo_token, other_merchant, fake_claude, spoken):
    """API surface: Supabase JWT on all routes, every table merchant-scoped. Another merchant must never get the
    demo merchant's spoken answer, before or after the demo merchant has played it once."""
    out = await ask_once(api, demo_token, fake_claude)
    qid = out["query_id"]
    stranger = bearer(other_merchant["token"])
    assert (await api.post("/voice/tts", json={"query_id": qid}, headers=stranger)).status_code == 404
    assert (await api.post("/voice/tts", json={"query_id": qid}, headers=bearer(demo_token))).status_code == 200
    r = await api.post("/voice/tts", json={"query_id": qid}, headers=stranger)
    assert r.status_code == 404, f"other merchant got {len(r.content)} bytes of the demo merchant's audio"
