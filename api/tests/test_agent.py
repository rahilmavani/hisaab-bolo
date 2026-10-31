"""M6: agent tools, number guard, components, pending actions, voice endpoints.

The Claude client is replaced by a scripted fake so the loop is deterministic;
real-API checks are marked `live` (run with `pytest -m live`).
"""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services.agent.runner import number_words, numbers_in, ungrounded
from app.services.agent.tools import romanize

from .conftest import bearer


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_number_guard_helpers():
    assert numbers_in("₹12,100 from 121 payments at 8:30") == {12100, 121, 8, 30}
    assert numbers_in("कुल ₹१२,१००") == {12100}
    assert number_words("राम ने पिछले चार महीने") == {4}
    assert number_words("pichhle char mahine") == {4}
    allowed = numbers_in(json.dumps({"total_rupees": 12100, "payments": 121}))
    assert not ungrounded("Ram ne ₹12,100 diye, 121 payments", allowed)
    assert ungrounded("Ram ne ₹12,500 diye", allowed) == {12500}


@pytest.mark.parametrize("dev,roman", [("राम", "ram"), ("शर्मा ट्रेडर्स", "sharma tredars"), ("मीना", "meena"),
                                       ("प्रिया", "priya"), ("संजय", "sanjay")])
def test_romanize(dev, roman):
    assert romanize(dev) == roman


# ---------------------------------------------------------------------------
# Scripted fake Claude
# ---------------------------------------------------------------------------
def tool_use(name, inp, i=1):
    return SimpleNamespace(type="tool_use", id=f"tu{i}", name=name, input=inp)


def final(**answer):
    base = {"display": "", "speech": "", "lang": "hi-IN", "components": [], "follow_ups": []}
    return SimpleNamespace(type="text", text=json.dumps(base | answer, ensure_ascii=False))


class FakeClaude:
    """Each step is a function (messages, model) -> list of content blocks."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        content = self.steps.pop(0)(kw["messages"], kw["model"])
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=500,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=content, stop_reason=stop, usage=usage)


def last_tool_result(messages) -> dict:
    return json.loads(messages[-1]["content"][-1]["content"])


@pytest.fixture
def fake_claude(monkeypatch):
    holder = {}

    def install(steps):
        fake = FakeClaude(steps)
        holder["fake"] = fake
        monkeypatch.setattr("app.services.agent.runner.client", lambda: fake)
        return fake
    return install


def ram_steps(speech="राम ने पिछले 4 महीने में ₹12,100 दिए, 121 payments।"):
    today = date.today()
    return [
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use("get_payer_summary", {
            "payer_id": last_tool_result(m)["matches"][0]["payer_id"],
            "from": (today - timedelta(days=122)).isoformat(), "to": today.isoformat()}, 2)],
        lambda m, model: [final(display="Ram ne ₹12,100 diye, 121 payments.", speech=speech,
                                components=[{"type": "payer_card", "result_id": last_tool_result(m)["result_id"]},
                                            {"type": "table", "result_id": "r1"}],
                                follow_ups=["Show all 121 payments", "Compare 999 years"])],
    ]


async def test_ask_happy_path(api, demo_token, fake_claude, db):
    fake = fake_claude(ram_steps())
    r = await api.post("/ask", json={"text": "राम ने पिछले चार महीने में कितना दिया?"}, headers=bearer(demo_token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["speech"].startswith("राम") and out["fallback"] is False and out["model"] == "claude-haiku-5-5"
    # table on a find_payer result is not an allowed pairing and is dropped
    assert [c["type"] for c in out["components"]] == ["payer_card"]
    card = out["components"][0]["data"]
    assert card["name"] == "Ram" and card["payments"] == 121 and card["total_paise"] == 12_100_00
    assert out["follow_ups"] == ["Show all 121 payments"]  # "Compare 999 years" is not grounded
    assert len(fake.calls) == 3
    first = fake.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert first["output_config"]["format"]["type"] == "json_schema"
    question_turn = first["messages"][0]["content"]  # the loop appends to the same list later
    assert "Today is" in question_turn and "IST" in question_turn and "Answer language: Hinglish" in question_turn
    assert "merchant_id" not in json.dumps([t["input_schema"] for t in first["tools"]])
    row = db.execute("select * from agent_queries where id = %s", (out["query_id"],)).fetchone()
    assert row["latency_ms"] >= 0 and row["cost_paise"] > 0
    assert [c["tool"] for c in row["tool_calls"]["calls"]] == ["find_payer", "get_payer_summary"]


async def test_number_guard_retries_on_fallback_model(api, demo_token, fake_claude):
    bad = ram_steps(speech="राम ने ₹99,999 दिए।")
    good = ram_steps()
    fake = fake_claude(bad + good)
    # The good answer's "4 महीने" is grounded by the question, not by whatever digits the data happens to hold.
    out = (await api.post("/ask", json={"text": "Ram ne pichhle 4 mahine mein kitna diya?"},
                          headers=bearer(demo_token))).json()
    assert out["model"] == "claude-sonnet-5-5" and out["fallback"] is False and "12,100" in out["speech"]
    assert [c["model"] for c in fake.calls] == ["claude-haiku-5-5"] * 3 + ["claude-sonnet-5-5"] * 3


async def test_number_guard_falls_back_to_data_without_speech(api, demo_token, fake_claude):
    fake_claude(ram_steps(speech="₹99,999") + ram_steps(speech="₹88,888"))
    out = (await api.post("/ask", json={"text": "Ram ne kitna diya?"}, headers=bearer(demo_token))).json()
    assert out["fallback"] is True and out["speech"] is None
    assert {c["type"] for c in out["components"]} >= {"payer_card"}


async def test_at_most_four_turns(api, demo_token, fake_claude):
    loop = lambda m, model: [tool_use("find_payer", {"name": "Ram"})]  # noqa: E731
    fake = fake_claude([loop, loop, loop, lambda m, model: [final(display="Ram mil gaye.", speech="")]])
    out = (await api.post("/ask", json={"text": "Ram"}, headers=bearer(demo_token))).json()
    assert len(fake.calls) == 4 and fake.calls[-1]["tool_choice"] == {"type": "none"}
    assert out["display"] == "Ram mil gaye."


async def test_write_tool_creates_pending_action_only(api, other_merchant, fake_claude, db):
    token = other_merchant["token"]
    due = (date.today() + timedelta(days=5)).isoformat()
    fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Asha Test"})],
        lambda m, model: [tool_use("create_invoice", {
            "payer_id": last_tool_result(m)["matches"][0]["payer_id"], "amount": 750, "due_date": due,
            "items": ["Gift hampers"], "email": None}, 2)],
        lambda m, model: [final(display="Invoice ready, ₹750. Confirm karein?", speech="",
                                components=[{"type": "confirm_action", "result_id": "r2"}])],
    ])
    before = db.execute("select count(*) as n from invoices").fetchone()["n"]
    out = (await api.post("/ask", json={"text": "Asha ka 750 ka invoice bana do"}, headers=bearer(token))).json()
    assert db.execute("select count(*) as n from invoices").fetchone()["n"] == before
    action = out["pending_action"]
    assert action and action["tool"] == "create_invoice" and action["summary"]["amount"] == "₹750"
    assert out["components"][0]["type"] == "confirm_action"

    r = await api.post(f"/actions/{action['token']}/confirm", headers=bearer(token))
    assert r.status_code == 200, r.text
    inv = r.json()["invoice"]
    assert inv["amount_paise"] == 750_00 and inv["payer_name"] == "Asha Test" and inv["link_url"]
    assert (await api.post(f"/actions/{action['token']}/confirm", headers=bearer(token))).status_code == 409


async def test_confirm_is_merchant_scoped_and_expires(api, demo_token, other_merchant, fake_claude, db):
    fake_claude([
        lambda m, model: [tool_use("list_dues", {"status": "overdue"})],
        lambda m, model: [tool_use("send_reminder", {"invoice_id": last_tool_result(m)["invoices"][0]["invoice_id"]}, 2)],
        lambda m, model: [final(display="Reminder ready.", speech="")],
    ])
    out = (await api.post("/ask", json={"text": "Sharma ko reminder bhejo"}, headers=bearer(demo_token))).json()
    token = out["pending_action"]["token"]
    assert (await api.post(f"/actions/{token}/confirm", headers=bearer(other_merchant["token"]))).status_code == 404
    db.execute("update pending_actions set expires_at = now() - interval '1 minute' where token = %s", (token,))
    assert (await api.post(f"/actions/{token}/confirm", headers=bearer(demo_token))).status_code == 410


async def test_tool_errors_are_returned_to_the_model(api, demo_token, fake_claude):
    fake = fake_claude([
        lambda m, model: [tool_use("get_payer_summary", {"payer_id": "not-a-uuid", "from": "2026-01-01", "to": "2026-01-31"})],
        lambda m, model: [final(display="Kaunse customer?", speech="")],
    ])
    await api.post("/ask", json={"text": "Kitna diya?"}, headers=bearer(demo_token))
    result = fake.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "find_payer" in json.loads(result["content"])["error"]


async def test_chips_for_ambiguous_names(api, demo_token, fake_claude):
    fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Rekha"})],
        lambda m, model: [final(display="Kaunsi Rekha?", speech="", components=[{"type": "chips", "result_id": "r1"}])],
    ])
    out = (await api.post("/ask", json={"text": "Rekha ne kitna diya"}, headers=bearer(demo_token))).json()
    chips = out["components"][0]
    assert chips["type"] == "chips" and len(chips["data"]["choices"]) >= 1


async def test_ask_needs_auth_and_text(api, demo_token):
    assert (await api.post("/ask", json={"text": "hi"})).status_code == 401
    assert (await api.post("/ask", json={"text": ""}, headers=bearer(demo_token))).status_code == 422


async def test_voice_ask_rejects_empty_audio(api, demo_token, monkeypatch):
    async def fake_transcribe(audio, ct):
        return {"transcript": "", "language_code": None}
    monkeypatch.setattr("app.services.sarvam.transcribe", fake_transcribe)
    r = await api.post("/voice/ask", files={"audio": ("q.webm", b"x" * 10, "audio/webm")}, headers=bearer(demo_token))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Live (real Anthropic + Sarvam)
# ---------------------------------------------------------------------------
@pytest.mark.live
async def test_live_voice_round_trip(api, demo_token):
    from app.services import sarvam

    audio = await sarvam.speak("राम ने पिछले चार महीने में कितना दिया?", "hi-IN")
    r = await api.post("/voice/ask", files={"audio": ("q.mp3", audio, "audio/mpeg")}, headers=bearer(demo_token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert "राम" in out["transcript"] or "Ram" in out["transcript"]
    assert "12,100" in out["display"] and "121" in out["display"] and not out["fallback"]
    tts = await api.post("/voice/tts", json={"query_id": out["query_id"]}, headers=bearer(demo_token))
    assert tts.status_code == 200 and tts.headers["content-type"] == "audio/mpeg" and len(tts.content) > 5000
