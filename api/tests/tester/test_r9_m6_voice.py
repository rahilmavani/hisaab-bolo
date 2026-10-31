"""Tester round 9, M6 voice: /voice/ask with Sarvam and Claude stubbed.

Spec (docs/ARCHITECTURE.md): voice path 1-7; API copy "Didn't catch that. Hold the mic and speak, or type your
question." (agent.py); Risks: "Wi-Fi or mic failure: the text fallback".
"""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("transcript", ["।", "...", " ? "])
async def test_a_transcript_with_no_words_gets_didnt_catch_that(api, demo_token, monkeypatch, fake_claude, transcript):
    """Background noise can come back as punctuation only. That is "didn't catch that", not a paid agent run."""
    stt(monkeypatch, transcript)
    fake = fake_claude([lambda m, model: [final(display="Kya poochna chahte hain?", speech="")]] * 2)
    r = await api.post("/voice/ask", files={"audio": ("q.webm", b"x" * 2000, "audio/webm")}, headers=bearer(demo_token))
    assert r.status_code == 422 and "Didn't catch that" in r.json()["detail"], (r.status_code, r.text[:200])
    assert fake.calls == []


async def test_voice_english_transcript_gets_an_english_spoken_answer(api, demo_token, monkeypatch, fake_claude, db):
    """UI copy "Hindi · English · Hinglish": an English voice question is answered and spoken in English."""
    stt(monkeypatch, "How much did Ram pay in the last four months?", "en-IN")
    today = db.execute("select app_today() as d").fetchone()["d"]
    from app.services.agent.runner import _months_back
    fake = fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use("get_payer_summary", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                         "from": str(_months_back(today, 4)), "to": str(today)}, 2)],
        lambda m, model: [final(display="Ram paid you ₹12,100 in the last 4 months: 121 payments.",
                                speech="Ram paid you ₹12,100 in the last 4 months.", lang="en-IN",
                                components=[{"type": "payer_card", "result_id": "r2"}])],
    ])
    r = await api.post("/voice/ask", files={"audio": ("q.webm", b"x" * 2000, "audio/webm")}, headers=bearer(demo_token))
    assert r.status_code == 200, r.text
    out = r.json()
    turn = next(m["content"] for m in fake.calls[0]["messages"] if isinstance(m["content"], str))
    assert "Answer language: English" in turn and turn.rstrip().endswith("Voice transcript: How much did Ram pay in the last four months?")
    assert out["lang"] == "en-IN" and out["speech"] and not out["fallback"]
    assert out["components"][0]["data"]["payments"] == 121
