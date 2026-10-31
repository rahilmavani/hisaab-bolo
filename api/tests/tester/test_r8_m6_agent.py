"""Tester round 8, M6 "Ask Hisaab": the agent loop, number guard, pending actions and tools, with the model stubbed.

Spec (docs/ARCHITECTURE.md):
  - Moment 1: "Ram ne pichhle 4 mahine mein kitna diya?" is answered with a spoken reply plus the payments behind it.
  - Agent: "Typed tools. Write actions create a pending action that needs a confirm tap. The model never writes."
  - Agent tools: "merchant_id is injected server-side and is never a parameter".
  - Voice path 4: "Claude tool loop, at most 4 turns ... The prompt includes today's date in IST."
  - Voice path 6: "Number guard: every number in `speech` must appear in a tool result; otherwise retry once on
    Sonnet, then fall back to showing the data with no speech."
  - Dynamic UI: "The model picks a type and a tool-result ID, and the server fills the rows."
  - Risks: "Speech-to-text mishears names: trigram search plus disambiguation chips."
  - UI copy (ask-panel.tsx): header "Hindi · English · Hinglish"; "Spoken back in Hinglish/English".
  - Pitch deck slide 5: a Hinglish question is "Spoken back in Hinglish".

Writes use the throwaway `other_merchant`; the demo merchant is only read (pending actions it gets are never confirmed).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.services import email as email_svc
from app.services.agent.runner import _months_back, range_starts, reply_language

from ..conftest import bearer


# ---------------------------------------------------------------------------
# Scripted fake Claude (same shape as tests/test_agent.py)
# ---------------------------------------------------------------------------
def tool_use(name, inp, i=1):
    return SimpleNamespace(type="tool_use", id=f"tu{i}", name=name, input=inp)


def final(**answer):
    base = {"display": "", "speech": "", "lang": "hi-IN", "components": [], "follow_ups": []}
    return SimpleNamespace(type="text", text=json.dumps(base | answer, ensure_ascii=False))


class FakeClaude:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        step = self.steps.pop(0)
        content = step(kw["messages"], kw["model"])
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=500,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=content, stop_reason=stop, usage=usage)


def raises(exc):
    def step(m, model):
        raise exc
    return step


def last_tool_result(messages) -> dict:
    return json.loads(messages[-1]["content"][-1]["content"])


def question_turn(fake) -> str:
    """The user turn the model got for the current question (the loop appends to the same list later)."""
    msgs = fake.calls[0]["messages"]
    return next(m["content"] for m in reversed(msgs) if m["role"] == "user" and isinstance(m["content"], str))


@pytest.fixture
def fake_claude(monkeypatch):
    def install(steps):
        fake = FakeClaude(steps)
        monkeypatch.setattr("app.services.agent.runner.client", lambda: fake)
        return fake
    return install


def ram_summary_steps(frm: str, to: str, speech: str, display: str = "Ram ne September mein ₹2,900 diye."):
    return [
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use("get_payer_summary", {
            "payer_id": last_tool_result(m)["matches"][0]["payer_id"], "from": frm, "to": to}, 2)],
        lambda m, model: [final(display=display, speech=speech,
                                components=[{"type": "payer_card", "result_id": last_tool_result(m)["result_id"]}])],
    ]


def models_called(fake) -> list[str]:
    return [c["model"] for c in fake.calls]


def app_today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


# ---------------------------------------------------------------------------
# Dates and language (pure)
# ---------------------------------------------------------------------------
def test_range_starts_match_spec_phrases():
    line = range_starts(date(2026, 10, 31))
    assert "last 4 months: 2026-06-30" in line  # "pichhle 4 mahine" from 31 Oct
    assert "this week: 2026-10-26" in line  # "is hafte" = from Monday
    assert "this month: 2026-10-01" in line
    assert _months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert _months_back(date(2028, 3, 31), 1) == date(2028, 2, 29)
    assert _months_back(date(2026, 1, 15), 4) == date(2025, 9, 15)


@pytest.mark.parametrize("q", [
    "Ram ne pichhle 4 mahine mein kitna diya?", "Aaj kitna collection hua?", "Kiske payment pending hain?",
    "Sharma Traders ko reminder bhej do", "राम ने पिछले चार महीने में कितना दिया?", "Is hafte ka hisaab batao",
])
def test_hinglish_and_hindi_questions_get_hinglish_replies(q):
    assert reply_language(q) == "hi"


@pytest.mark.parametrize("q", [
    "How much did Ram pay in the last 4 months?", "Do I have any overdue invoices?", "Send a reminder to Sharma Traders",
    "Mark Priya Caterers as monthly recurring for 3200",
])
def test_english_questions_get_english_replies(q):
    assert reply_language(q) == "en"


@pytest.mark.parametrize("q", ["Sharma Traders se payment mila?", "Ram se paisa aaya?", "Priya Caterers ne payment kiya?"])
def test_everyday_hinglish_without_question_words_is_still_hinglish(q):
    """Header copy promises Hindi · English · Hinglish and slide 5 shows a Hinglish question spoken back in
    Hinglish. Common Hinglish yes/no questions ("se ... mila?", "paisa aaya?") must not be answered in English."""
    assert reply_language(q) == "hi"


async def test_question_turn_carries_ist_date_ranges_and_origin(api, demo_token, fake_claude, db):
    today = app_today(db)
    fake = fake_claude(ram_summary_steps(str(today), str(today), speech="", display="Ram ne aaj ₹100 diye."))
    r = await api.post("/ask", json={"text": "Ram ne aaj kitna diya?"}, headers=bearer(demo_token))
    assert r.status_code == 200, r.text
    turn = question_turn(fake)
    assert today.strftime("%d %B %Y") in turn and "IST" in turn
    assert f"last 4 months: {_months_back(today, 4)}" in turn
    assert "Typed question: Ram ne aaj kitna diya?" in turn


# ---------------------------------------------------------------------------
# Number guard
# ---------------------------------------------------------------------------
async def test_guard_checks_display_too_and_retries_on_sonnet(api, demo_token, fake_claude):
    bad = ram_summary_steps("2026-09-01", "2026-09-30", speech="राम ने सितंबर में ₹2,900 दिए।",
                            display="Ram ne September mein ₹3,900 diye.")
    good = ram_summary_steps("2026-09-01", "2026-09-30", speech="राम ने सितंबर में ₹2,900 दिए।")
    fake = fake_claude(bad + good)
    out = (await api.post("/ask", json={"text": "September mein Ram ne kitna diya?"}, headers=bearer(demo_token))).json()
    assert models_called(fake) == ["claude-haiku-5-5"] * 3 + ["claude-sonnet-5-5"] * 3
    assert out["model"] == "claude-sonnet-5-5" and "2,900" in out["display"] and not out["fallback"]


async def test_guard_does_not_accept_a_number_that_is_only_in_the_question(api, demo_token, fake_claude):
    """Spec: every number in speech must appear in a TOOL RESULT. A leading question ("5000 diye na?") must not
    ground a wrong total: Ram paid ₹2,900 in September, so "₹5,000 दिए" has to trigger the Sonnet retry."""
    wrong = ram_summary_steps("2026-09-01", "2026-09-30", speech="हाँ, राम ने सितंबर में ₹5,000 दिए।",
                              display="Haan, Ram ne September mein ₹5,000 diye.")
    right = ram_summary_steps("2026-09-01", "2026-09-30", speech="नहीं, राम ने सितंबर में ₹2,900 दिए।")
    fake = fake_claude(wrong + right)
    out = (await api.post("/ask", json={"text": "Ram ne September mein 5000 diye na?"}, headers=bearer(demo_token))).json()
    assert "claude-sonnet-5-5" in models_called(fake), "a ₹5,000 total grounded only by the question was spoken"
    assert "5,000" not in (out["speech"] or "")


async def test_guard_checks_numbers_written_as_words(api, demo_token, fake_claude):
    """The real model writes some numbers as words in speech ("छह payments", "दो invoices", "nine दिन" were seen
    in this round). A wrong amount in words must not slip past the guard: Ram paid ₹2,900 in September."""
    wrong = ram_summary_steps("2026-09-01", "2026-09-30", speech="राम ने सितंबर में पाँच हज़ार रुपये दिए।",
                              display="Ram ne September mein payments kiye.")
    right = ram_summary_steps("2026-09-01", "2026-09-30", speech="राम ने सितंबर में ₹2,900 दिए।")
    fake = fake_claude(wrong + right)
    out = (await api.post("/ask", json={"text": "September mein Ram ne kitna diya?"}, headers=bearer(demo_token))).json()
    assert "claude-sonnet-5-5" in models_called(fake), f"spoken words for 5,000 passed the guard: {out['speech']}"


async def test_guard_does_not_count_digits_inside_ids(api, demo_token, fake_claude, db):
    """payer_id / invoice_id UUIDs are in the tool results the guard reads; their digit runs are not amounts."""
    ram = db.execute("select p.id from payers p join merchants m on m.id = p.merchant_id "
                     "where m.name = 'Gupta Kirana Store' and p.display_name = 'Ram'").fetchone()["id"]
    runs = sorted({r.lstrip("0") for r in re.findall(r"\d{3,}", str(ram).replace("-", " "))} - {""}, key=len)
    if not runs:
        pytest.skip(f"Ram's id {ram} has no 3-digit run this seed")
    fake_number = runs[-1]
    wrong = ram_summary_steps("2026-09-01", "2026-09-30", speech=f"राम ने सितंबर में ₹{fake_number} दिए।",
                              display=f"Ram ne September mein ₹{fake_number} diye.")
    right = ram_summary_steps("2026-09-01", "2026-09-30", speech="राम ने सितंबर में ₹2,900 दिए।")
    fake = fake_claude(wrong + right)
    await api.post("/ask", json={"text": "September mein Ram ne kitna diya?"}, headers=bearer(demo_token))
    assert "claude-sonnet-5-5" in models_called(fake), f"₹{fake_number} (digits from Ram's UUID {ram}) passed the guard"


async def test_guard_fallback_shows_data_without_speech(api, demo_token, fake_claude):
    fake_claude(ram_summary_steps("2026-09-01", "2026-09-30", speech="₹99,999")
                + ram_summary_steps("2026-09-01", "2026-09-30", speech="₹88,888"))
    out = (await api.post("/ask", json={"text": "September mein Ram ne kitna diya?"}, headers=bearer(demo_token))).json()
    assert out["fallback"] is True and out["speech"] is None
    card = next(c for c in out["components"] if c["type"] == "payer_card")["data"]
    assert card["payments"] == 29 and card["total_paise"] == 2_900_00


async def test_guard_fallback_answers_in_the_question_language(api, demo_token, fake_claude):
    """A Hindi question that falls back still gets its display in Hinglish, not a switch to English."""
    fake_claude(ram_summary_steps("2026-09-01", "2026-09-30", speech="₹99,999")
                + ram_summary_steps("2026-09-01", "2026-09-30", speech="₹88,888"))
    out = (await api.post("/ask", json={"text": "राम ने सितंबर में कितना दिया?"}, headers=bearer(demo_token))).json()
    assert out["fallback"] is True
    assert out["display"] != "Here is what I found.", "Hindi question got an English fallback line"


async def test_english_question_with_hinglish_answer_is_not_labelled_english(api, demo_token, fake_claude):
    """Seen live 2/2: "Mark Priya Caterers as monthly recurring for 3200" came back with Hinglish display AND
    speech; the runner copies display into speech and labels it en-IN, so the UI says "Spoken back in English"
    over Hinglish text. Either the reply is English or it is not labelled English."""
    hinglish_steps = [
        lambda m, model: [tool_use("find_payer", {"name": "Priya Caterers"})],
        lambda m, model: [tool_use("set_recurring", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                     "cadence": "monthly", "amount": 3200, "email": None}, 2)],
        lambda m, model: [final(display="Priya Caterers ko monthly recurring mark karne ka action ready hai. Confirm karein.",
                                speech="Priya Caterers ko monthly recurring mark karne ka action ready hai.",
                                lang="hi-IN", components=[{"type": "confirm_action", "result_id": "r2"}])],
    ]
    # A Hinglish answer to an English question earns a retry on the fallback model (scripted the same here).
    fake = fake_claude(hinglish_steps + hinglish_steps)
    out = (await api.post("/ask", json={"text": "Mark Priya Caterers as monthly recurring for 3200"},
                          headers=bearer(demo_token))).json()
    assert fake.calls
    hinglish = reply_language(out["display"]) == "hi"
    assert not (hinglish and out["lang"] == "en-IN"), f"Hinglish answer labelled en-IN: {out['display']}"


# ---------------------------------------------------------------------------
# Names: unknown and ambiguous
# ---------------------------------------------------------------------------
async def test_unknown_name_is_not_answered_as_a_different_single_customer(api, demo_token):
    """Moment 1 tooling: find_payer("Ramesh") returns only "Ram" and says single_match=True, so the model answers
    "Ramesh" questions with Ram's money (seen live 3/3, speech never mentions the swap). Risks: "Speech-to-text
    mishears names: trigram search plus disambiguation chips" -- a different name should come back as a choice,
    not as a certain single match."""
    from app.db import connection, fetch_one
    from app.services.agent.tools import ToolContext, run_tool

    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        ctx = ToolContext(conn=conn, merchant_id=m["id"])
        out, _ = await run_tool(ctx, "find_payer", {"name": "Ramesh"})
        devanagari, _ = await run_tool(ctx, "find_payer", {"name": "रमेश"})
        nobody, _ = await run_tool(ctx, "find_payer", {"name": "Mohan"})
    assert nobody["matches"] == [] and nobody["single_match"] is False
    assert not (out["single_match"] and [x["name"] for x in out["matches"]] == ["Ram"]), out
    assert not (devanagari["single_match"] and devanagari["matches"][0]["name"] == "Ram"), devanagari


async def test_ambiguous_name_gives_chips_and_a_pick_is_passed_as_hint(api, other_merchant, fake_claude):
    token = other_merchant["token"]
    fake = fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Asha"})],
        lambda m, model: [final(display="Kaunsi Asha?", speech="", components=[{"type": "chips", "result_id": "r1"}])],
    ])
    out = (await api.post("/ask", json={"text": "Asha ne kitna diya?"}, headers=bearer(token))).json()
    result = json.loads(fake.calls[1]["messages"][-1]["content"][0]["content"])
    assert result["single_match"] is False and len(result["matches"]) == 2
    chips = out["components"][0]
    assert chips["type"] == "chips"
    picked = {c["label"]: c["payer_id"] for c in chips["data"]["choices"]}
    assert set(picked) == {"Asha Test", "Asha T (UPI)"}

    fake = fake_claude([lambda m, model: [final(display="Thik hai.", speech="")]])
    r = await api.post("/ask", json={"text": "Asha ne kitna diya? (Asha Test)", "payer_hint": picked["Asha Test"],
                                     "history": [{"question": "Asha ne kitna diya?", "answer": "Kaunsi Asha?"}]},
                       headers=bearer(token))
    assert r.status_code == 200, r.text
    assert f"The merchant picked this customer: Asha Test (payer_id {picked['Asha Test']})" in question_turn(fake)


async def test_payer_hint_from_another_merchant_is_ignored(api, demo_token, other_merchant, fake_claude):
    fake = fake_claude([lambda m, model: [final(display="Kaun?", speech="")]])
    r = await api.post("/ask", json={"text": "Iska hisaab?", "payer_hint": str(other_merchant["payer_a"])},
                       headers=bearer(demo_token))
    assert r.status_code == 200
    assert "Asha Test" not in question_turn(fake) and "picked this customer" not in question_turn(fake)


# ---------------------------------------------------------------------------
# Tools stay inside the merchant
# ---------------------------------------------------------------------------
async def test_tools_never_reach_another_merchants_data(api, demo_token, other_merchant, fake_claude, db):
    demo = db.execute("select id from merchants where name = 'Gupta Kirana Store'").fetchone()["id"]
    ram = db.execute("select id from payers where merchant_id = %s and display_name = 'Ram'", (demo,)).fetchone()["id"]
    inv = db.execute("select id from invoices where merchant_id = %s and status = 'overdue' limit 1", (demo,)).fetchone()["id"]
    today = app_today(db)
    fake = fake_claude([
        lambda m, model: [tool_use("get_payer_summary", {"payer_id": str(ram), "from": "2026-06-30", "to": str(today)}, 1),
                          tool_use("list_transactions", {"payer_id": None, "from": "2026-01-01", "to": str(today),
                                                         "limit": 200}, 2),
                          tool_use("send_reminder", {"invoice_id": str(inv)}, 3),
                          tool_use("create_invoice", {"payer_id": str(ram), "amount": 100, "due_date": str(today),
                                                      "items": [], "email": None}, 4)],
        lambda m, model: [final(display="Kuch nahi mila.", speech="", components=[{"type": "table", "result_id": "r1"}])],
    ])
    out = (await api.post("/ask", json={"text": "Ram ka hisaab"}, headers=bearer(other_merchant["token"]))).json()
    results = [json.loads(b["content"]) for b in fake.calls[1]["messages"][-1]["content"]]
    assert results[0] == {"error": "no such customer"}
    assert results[1]["payments"] == 3 and {r["customer"] for r in results[1]["rows"]} <= {"Asha Test", "Asha T (UPI)"}
    assert results[2] == {"error": "no such invoice"} and results[3] == {"error": "no such customer"}
    assert out["pending_action"] is None
    assert "merchant_id" not in json.dumps([c["tools"] for c in fake.calls][0])


# ---------------------------------------------------------------------------
# Write actions: pending, confirm, expiry, double confirm, cross-merchant
# ---------------------------------------------------------------------------
def invoice_steps(due: str, amount=750, email=None):
    return [
        lambda m, model: [tool_use("find_payer", {"name": "Asha Test"})],
        lambda m, model: [tool_use("create_invoice", {
            "payer_id": next(x["payer_id"] for x in last_tool_result(m)["matches"] if x["name"] == "Asha Test"),
            "amount": amount, "due_date": due, "items": ["Gift hampers"], "email": email}, 2)],
        lambda m, model: [final(display=f"₹{amount} ka invoice ready hai, confirm karein.", speech="")],
    ]


async def prepare_invoice(api, m, db, fake_claude, **kw) -> dict:
    fake_claude(invoice_steps((app_today(db) + timedelta(days=5)).isoformat(), **kw))
    out = (await api.post("/ask", json={"text": "Asha ka invoice bana do"}, headers=bearer(m["token"]))).json()
    assert out["pending_action"], out
    return out


async def test_model_forgetting_confirm_component_still_shows_confirm(api, other_merchant, fake_claude, db):
    out = await prepare_invoice(api, other_merchant, db, fake_claude)
    assert [c["type"] for c in out["components"]] == ["confirm_action"]
    assert out["components"][0]["data"]["token"] == out["pending_action"]["token"]


async def test_concurrent_double_confirm_creates_one_invoice(api, other_merchant, fake_claude, db):
    out = await prepare_invoice(api, other_merchant, db, fake_claude)
    token = out["pending_action"]["token"]
    before = db.execute("select count(*) n from invoices where merchant_id = %s", (other_merchant["merchant_id"],)).fetchone()["n"]
    r1, r2 = await asyncio.gather(*[api.post(f"/actions/{token}/confirm", headers=bearer(other_merchant["token"]))
                                    for _ in range(2)])
    assert sorted([r1.status_code, r2.status_code]) == [200, 409], (r1.text, r2.text)
    after = db.execute("select count(*) n from invoices where merchant_id = %s", (other_merchant["merchant_id"],)).fetchone()["n"]
    assert after == before + 1


async def test_pending_action_expires_15_minutes_later_on_the_app_clock(api, other_merchant, fake_claude, db, restore_clock):
    out = await prepare_invoice(api, other_merchant, db, fake_claude)
    token = out["pending_action"]["token"]
    row = db.execute("select expires_at - app_now() as left from pending_actions where token = %s", (token,)).fetchone()
    assert timedelta(minutes=14) < row["left"] <= timedelta(minutes=15)
    db.execute("update app_clock set override_now = app_now() + interval '16 minutes', set_at = now() where id = 1")
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(other_merchant["token"]))
    assert r.status_code == 410 and "expired" in r.json()["detail"]
    assert db.execute("select count(*) n from invoices where merchant_id = %s",
                      (other_merchant["merchant_id"],)).fetchone()["n"] == 0


async def test_cross_merchant_confirm_is_404_and_leaves_the_action_usable(api, demo_token, other_merchant, fake_claude, db):
    out = await prepare_invoice(api, other_merchant, db, fake_claude)
    token = out["pending_action"]["token"]
    assert (await api.post(f"/actions/{token}/confirm", headers=bearer(demo_token))).status_code == 404
    assert (await api.post("/actions/pa_doesnotexist/confirm", headers=bearer(demo_token))).status_code == 404
    assert (await api.post(f"/actions/{token}/confirm")).status_code == 401
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(other_merchant["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["invoice"]["payer_name"] == "Asha Test"


async def test_reminder_needs_confirm_and_a_paid_invoice_refuses_then_allows_retry(api, other_merchant, fake_claude, db):
    m = other_merchant
    due = (app_today(db) + timedelta(days=3)).isoformat()
    r = await api.post("/invoices", json={"payer_id": str(m["payer_a"]), "due_date": due, "amount_paise": 500_00,
                                          "email": "asha@example.com", "send_email": False}, headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    inv = r.json()
    fake_claude([
        lambda mm, model: [tool_use("list_dues", {"status": "open"})],
        lambda mm, model: [tool_use("send_reminder", {"invoice_id": last_tool_result(mm)["invoices"][0]["invoice_id"]}, 2)],
        lambda mm, model: [final(display="Reminder ready, confirm karein.", speech="")],
    ])
    email_svc.SENT.clear()
    reminders = lambda: db.execute("select count(*) n from reminders where invoice_id = %s", (inv["id"],)).fetchone()["n"]  # noqa: E731
    out = (await api.post("/ask", json={"text": "Asha ko reminder bhejo"}, headers=bearer(m["token"]))).json()
    token = out["pending_action"]["token"]
    assert reminders() == 0 and not email_svc.SENT
    db.execute("update invoices set status = 'paid', paid_at = now() where id = %s", (inv["id"],))
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(m["token"]))
    assert r.status_code == 409 and reminders() == 0
    assert db.execute("select confirmed_at from pending_actions where token = %s", (token,)).fetchone()["confirmed_at"] is None
    db.execute("update invoices set status = 'sent', paid_at = null where id = %s", (inv["id"],))
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(m["token"]))
    assert r.status_code == 200, r.text
    assert reminders() == 1 and len(email_svc.SENT) == 1
    assert (await api.post(f"/actions/{token}/confirm", headers=bearer(m["token"]))).status_code == 409


async def test_set_recurring_is_pending_until_confirmed(api, other_merchant, fake_claude, db):
    m = other_merchant
    fake_claude([
        lambda mm, model: [tool_use("set_recurring", {"payer_id": str(m["payer_b"]), "cadence": "weekly", "amount": 250,
                                                      "email": None})],
        lambda mm, model: [final(display="Weekly ₹250, confirm karein.", speech="")],
    ])
    out = (await api.post("/ask", json={"text": "Asha T ko weekly 250 recurring karo"}, headers=bearer(m["token"]))).json()
    action = out["pending_action"]
    assert action["tool"] == "set_recurring" and action["summary"]["delivery"].startswith("Tracking only")
    sched = lambda: db.execute("select count(*) n from recurring_schedules where payer_id = %s and active",  # noqa: E731
                               (m["payer_b"],)).fetchone()["n"]
    assert sched() == 0
    r = await api.post(f"/actions/{action['token']}/confirm", headers=bearer(m["token"]))
    assert r.status_code == 200, r.text
    assert sched() == 1 and "tracking only" in r.json()["message"]


async def test_send_reminder_tool_refuses_invoices_that_cannot_be_reminded(api, other_merchant, fake_claude, db):
    m = other_merchant
    due = (app_today(db) + timedelta(days=3)).isoformat()
    r = await api.post("/invoices", json={"payer_id": str(m["payer_b"]), "due_date": due, "amount_paise": 300_00},
                       headers=bearer(m["token"]))
    assert r.status_code == 201, r.text
    no_email = r.json()["id"]
    fake = fake_claude([
        lambda mm, model: [tool_use("send_reminder", {"invoice_id": no_email})],
        lambda mm, model: [final(display="Email nahi hai.", speech="")],
    ])
    out = (await api.post("/ask", json={"text": "reminder bhejo"}, headers=bearer(m["token"]))).json()
    err = json.loads(fake.calls[1]["messages"][-1]["content"][0]["content"])
    assert "no email" in err["error"] and out["pending_action"] is None


# ---------------------------------------------------------------------------
# Claude failures
# ---------------------------------------------------------------------------
def _conn_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


async def test_both_models_down_gives_a_polite_answer_and_is_logged(api, demo_token, fake_claude, db):
    fake_claude([raises(_conn_error()), raises(_conn_error())])
    r = await api.post("/ask", json={"text": "Ram ne kitna diya?"}, headers=bearer(demo_token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["fallback"] is True and out["speech"] is None and out["components"] == []
    # Hinglish question, so the apology is in Hinglish (round 8 finding 14).
    assert out["display"].startswith("Maaf kijiye") and out["lang"] == "hi-IN"
    row = db.execute("select tool_calls from agent_queries where id = %s", (out["query_id"],)).fetchone()
    assert [a.get("error") is not None for a in row["tool_calls"]["attempts"]] == [True, True]


async def test_haiku_down_sonnet_answers(api, demo_token, fake_claude):
    fake = fake_claude([raises(_conn_error())] + ram_summary_steps("2026-09-01", "2026-09-30",
                                                                  speech="राम ने सितंबर में ₹2,900 दिए।"))
    out = (await api.post("/ask", json={"text": "September mein Ram ne kitna diya?"}, headers=bearer(demo_token))).json()
    assert out["model"] == "claude-sonnet-5-5" and not out["fallback"] and out["speech"]
    assert models_called(fake)[0] == "claude-haiku-5-5"


async def test_non_json_answer_retries_on_sonnet(api, demo_token, fake_claude):
    fake = fake_claude([lambda m, model: [SimpleNamespace(type="text", text="Ram ne 2900 diye")]]
                       + ram_summary_steps("2026-09-01", "2026-09-30", speech="राम ने सितंबर में ₹2,900 दिए।"))
    out = (await api.post("/ask", json={"text": "September mein Ram ne kitna diya?"}, headers=bearer(demo_token))).json()
    assert out["model"] == "claude-sonnet-5-5" and not out["fallback"]
    assert models_called(fake) == ["claude-haiku-5-5"] + ["claude-sonnet-5-5"] * 3


# ---------------------------------------------------------------------------
# History / follow-ups
# ---------------------------------------------------------------------------
async def test_history_keeps_last_three_turns_in_order(api, demo_token, fake_claude):
    fake = fake_claude([lambda m, model: [final(display="Ok.", speech="")]])
    hist = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(5)]
    r = await api.post("/ask", json={"text": "Aur pichhle mahine?", "history": hist}, headers=bearer(demo_token))
    assert r.status_code == 200
    msgs = fake.calls[0]["messages"]
    assert [(m["role"], m["content"]) for m in msgs[:6]] == [
        ("user", "q2"), ("assistant", "a2"), ("user", "q3"), ("assistant", "a3"), ("user", "q4"), ("assistant", "a4")]
    assert msgs[6]["content"].endswith("Typed question: Aur pichhle mahine?")
    too_many = [{"question": "q", "answer": "a"}] * 7
    assert (await api.post("/ask", json={"text": "x", "history": too_many}, headers=bearer(demo_token))).status_code == 422
    assert (await api.post("/ask", json={"text": "x" * 501}, headers=bearer(demo_token))).status_code == 422


async def test_follow_ups_with_ungrounded_numbers_are_dropped(api, demo_token, fake_claude):
    steps = ram_summary_steps("2026-09-01", "2026-09-30", speech="राम ने सितंबर में ₹2,900 दिए।")
    steps[-1] = lambda m, model: [final(display="Ram ne September mein ₹2,900 diye.", speech="राम ने ₹2,900 दिए।",
                                        follow_ups=["Show all 29 payments", "Show all 777 payments", "Compare", "x"])]
    fake_claude(steps)
    out = (await api.post("/ask", json={"text": "September mein Ram ne kitna diya?"}, headers=bearer(demo_token))).json()
    assert out["follow_ups"] == ["Show all 29 payments"]


# ---------------------------------------------------------------------------
# Components the server fills
# ---------------------------------------------------------------------------
async def test_components_rows_come_from_the_server_not_the_model(api, demo_token, fake_claude, db):
    today = app_today(db)
    fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use("list_transactions", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                         "from": _months_back(today, 4).isoformat(), "to": str(today),
                                                         "limit": 200}, 2)],
        lambda m, model: [final(display="Ram ke 121 payments, ₹12,100.", speech="",
                                components=[{"type": "table", "result_id": "r2"}, {"type": "table", "result_id": "r2"},
                                            {"type": "payer_card", "result_id": "r2"}, {"type": "table", "result_id": "r9"},
                                            {"type": "stat_cards", "result_id": "r2"}])],
    ])
    out = (await api.post("/ask", json={"text": "Ram ke saare payments dikhao"}, headers=bearer(demo_token))).json()
    assert [c["type"] for c in out["components"]] == ["table", "stat_cards"]
    table = out["components"][0]["data"]
    assert table["total_rows"] == 121 and len(table["rows"]) == 121 and table["total_paise"] == 12_100_00
    assert all(r["customer"] == "Ram" and r["amount_paise"] == 100_00 for r in table["rows"])


async def test_sales_chart_covers_the_same_window_as_the_totals(api, demo_token, db):
    """Spec: get_sales_summary -> line_chart + stat_cards. For "last 7 days" by week the stat card totals 7 days but
    the chart starts at the next Monday, so chart and card disagree."""
    from app.db import connection, fetch_one
    from app.services.agent.components import build_components
    from app.services.agent.tools import ToolContext, run_tool

    today = app_today(db)
    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        ctx = ToolContext(conn=conn, merchant_id=m["id"])
        out, _ = await run_tool(ctx, "get_sales_summary", {"from": str(today - timedelta(days=6)), "to": str(today),
                                                           "grain": "week"})
        comps = build_components([{"type": "line_chart", "result_id": out["result_id"]},
                                  {"type": "stat_cards", "result_id": out["result_id"]}], ctx)
    chart_total = sum(p["total_paise"] for p in comps[0]["data"]["points"])
    card_total = comps[1]["data"]["items"][0]["value_paise"]
    assert chart_total == card_total, f"chart ₹{chart_total / 100:,.0f} vs card ₹{card_total / 100:,.0f}"
