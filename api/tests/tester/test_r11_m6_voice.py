"""Tester round 11, M6 voice: /voice/ask with Sarvam and Claude stubbed.

Spec (docs/ARCHITECTURE.md): moment 1 by voice ("answered with a spoken reply plus the payments behind it"); voice path
3 Sarvam STT, 6 number guard; "Web push-to-talk. A text box fallback hits the same agent."
"""

from __future__ import annotations

import json

import pytest

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


def steps(today, tool, display, speech, ctype):
    args = {"from": str(_months_back(today, 4)), "to": str(today)}
    if tool == "list_transactions":
        args["limit"] = 200
    return [
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use(tool, {"payer_id": last_tool_result(m)["matches"][0]["payer_id"], **args}, 2)],
        lambda m, model: [final(display=display, speech=speech, components=[{"type": ctype, "result_id": "r2"}])],
    ]


async def voice(api, token, history=None):
    data = {"history": json.dumps(history)} if history is not None else None
    return await api.post("/voice/ask", files={"audio": ("q.webm", b"x" * 4000, "audio/webm")}, data=data,
                          headers=bearer(token))


async def test_voice_moment1_with_mahinon_is_spoken_with_the_card(api, demo_token, monkeypatch, fake_claude, db):
    stt(monkeypatch, "राम ने पिछले चार महीनों में कितना दिया?")
    today = db.execute("select app_today() as d").fetchone()["d"]
    fake = fake_claude(steps(today, "get_payer_summary", "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments.",
                             "राम ने पिछले 4 महीनों में ₹12,100 दिए, 121 payments।", "payer_card") * 2)
    r = await voice(api, demo_token)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["transcript"].startswith("राम") and out["speech"] and not out["fallback"] and len(fake.calls) == 3
    card = next(c for c in out["components"] if c["type"] == "payer_card")["data"]
    assert (card["payments"], card["total_paise"]) == (121, 12_100_00)


async def test_voice_follow_up_carries_the_typed_moment1_range(api, demo_token, monkeypatch, fake_claude, db):
    """A spoken follow-up ("saare payments dikhao") after the typed moment-1 question: history arrives as a form field
    and may ground the "4" of the earlier range."""
    stt(monkeypatch, "राम के सारे payments दिखाओ")
    today = db.execute("select app_today() as d").fetchone()["d"]
    fake = fake_claude(steps(today, "list_transactions", "Ram ke pichhle 4 mahine ke saare 121 payments neeche hain.",
                             "राम के पिछले 4 महीने के सारे 121 payments नीचे हैं।", "table") * 2)
    out = (await voice(api, demo_token, [{"question": "Ram ne pichhle 4 mahine mein kitna diya?",
                                          "answer": "Ram ne ₹12,100 diye, 121 payments."}])).json()
    assert not out["fallback"] and len(fake.calls) == 3, (out.get("fallback"), len(fake.calls))
    table = next(c for c in out["components"] if c["type"] == "table")["data"]
    assert table["total_rows"] == 121


@pytest.mark.parametrize("transcript", ["।", "...", "  "])
async def test_voice_transcript_with_no_words_is_not_sent_to_the_model(api, demo_token, monkeypatch, fake_claude,
                                                                      transcript):
    stt(monkeypatch, transcript)
    fake = fake_claude([])
    r = await voice(api, demo_token)
    assert r.status_code == 422 and fake.calls == []
    assert r.json()["detail"].startswith("Didn't catch that")
