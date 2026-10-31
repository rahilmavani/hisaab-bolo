"""Tester round 9, M6 "Ask Hisaab": re-checks of the round-8 fixes plus new edge cases, with the model stubbed.

Spec (docs/ARCHITECTURE.md):
  - Moment 1: "Ram ne pichhle 4 mahine mein kitna diya?" is answered with a spoken reply plus the payments behind it.
  - Voice path 6: "Number guard: every number in `speech` must appear in a tool result; otherwise retry once on
    Sonnet, then fall back to showing the data with no speech."
  - Dynamic UI: "The model picks a type and a tool-result ID, and the server fills the rows."
  - get_sales_summary -> line_chart + stat_cards.
  - Agent: "Write actions create a pending action that needs a confirm tap. The model never writes."
  - Languages: Hindi, English, Hinglish (UI header "Hindi · English · Hinglish").
  - Risks: "Speech-to-text mishears names: trigram search plus disambiguation chips."

Seeded facts: Ram pays ₹100 daily around 8:30; 121 payments / ₹12,100 since 1 Jul; ₹2,900 / 29 payments in September.
Writes use the throwaway `other_merchant`; the demo merchant is only read.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.services.agent.runner import _months_back, range_starts, reply_language

from ..conftest import bearer


# ---------------------------------------------------------------------------
# Scripted fake Claude
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


@pytest.fixture
def fake_claude(monkeypatch):
    def install(steps):
        fake = FakeClaude(steps)
        monkeypatch.setattr("app.services.agent.runner.client", lambda: fake)
        return fake
    return install


def app_today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


def summary_steps(name: str, frm: str, to: str, display: str, speech: str, components=None):
    """find_payer(name) -> get_payer_summary(first match) -> final answer (components default: the payer card)."""
    return [
        lambda m, model: [tool_use("find_payer", {"name": name})],
        lambda m, model: [tool_use("get_payer_summary", {
            "payer_id": last_tool_result(m)["matches"][0]["payer_id"], "from": frm, "to": to}, 2)],
        lambda m, model: [final(display=display, speech=speech, lang="hi-IN",
                                components=components if components is not None
                                else [{"type": "payer_card", "result_id": "r2"}])],
    ]


async def ask(api, token, text, **body) -> dict:
    r = await api.post("/ask", json={"text": text, **body}, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Number guard: amounts the guard still can't see
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("question,display,speech", [
    # ₹12,100 is the truth; "one lakh" is a made-up amount written in Indian number words.
    ("Ram ne pichhle 4 mahine mein kitna diya?", "Ram ne pichhle 4 mahine mein ek lakh rupaye diye.",
     "राम ने पिछले 4 महीने में एक लाख रुपये दिए।"),
    ("How much did Ram pay in the last 4 months?", "Ram paid you one lakh rupees in the last 4 months.",
     "Ram paid you one lakh rupees in the last 4 months."),
    # 121 is the truth; "pachaas / पचास" (50) is made up.
    ("Ram ne pichhle 4 mahine mein kitne payment kiye?", "Ram ne pichhle 4 mahine mein pachaas payment kiye.",
     "राम ने पिछले 4 महीने में पचास payment किए।"),
])
async def test_guard_catches_made_up_amounts_in_lakh_and_tens_words(api, demo_token, fake_claude, db, question,
                                                                    display, speech):
    """Voice path 6: every number in speech must be grounded. Round 8 added number words, but the list stops at
    "हज़ार / thousand" and a few tens: lakh/crore and words like पचास (50) are not read as numbers at all, so a
    made-up "एक लाख" passes the guard and gets spoken."""
    today = app_today(db)
    frm = str(_months_back(today, 4))
    steps = summary_steps("Ram", frm, str(today), display, speech)
    fake_claude(steps + summary_steps("Ram", frm, str(today), display, speech))
    out = await ask(api, demo_token, question)
    assert out["fallback"] is True and out["speech"] is None, (
        f"made-up amount passed the number guard: display={out['display']!r} speech={out['speech']!r}")


@pytest.mark.parametrize("question,display", [
    ("Ram ne aaj 125 diye kya?", "Haan, Ram ne aaj ₹125 diye."),
    ("Did Ram pay 175 today?", "Yes, Ram paid you ₹175 today."),
])
async def test_guard_does_not_ground_an_amount_the_merchant_said_without_a_rupee_sign(api, demo_token, fake_claude, db,
                                                                                       question, display):
    """Round 8 fix: "an amount the merchant states is not evidence". It only holds for amounts over 366 or with ₹/rupaye;
    "Ram ne aaj 125 diye kya?" lets the model confirm ₹125 when the tool says ₹100. (Round amounts like 200 are
    only rejected by accident: see the scientific-notation test below.)"""
    today = str(app_today(db))
    steps = summary_steps("Ram", today, today, display, display)
    fake_claude(steps + summary_steps("Ram", today, today, display, display))
    out = await ask(api, demo_token, question)
    assert out["fallback"] is True and out["speech"] is None, f"echoed the merchant's amount: {out['display']!r}"


@pytest.mark.parametrize("question,days,template", [
    ("Ram ne pichhle 90 din mein kitna diya?", 90, "Ram ne pichhle 90 din mein ₹{total:,} diye, {n} payments."),
    ("How much did Ram pay in the last 60 days?", 60, "Ram paid you ₹{total:,} in the last 60 days ({n} payments)."),
])
async def test_a_round_day_count_from_the_question_may_be_repeated(api, demo_token, fake_claude, db, question, days,
                                                                   template):
    """Runner docstring + question_numbers(): small numbers from the question ("4 mahine", "8 baje") may be repeated
    back. question_numbers() joins str(Decimal.normalize()), which turns 90 into "9E+1"; the guard then reads 9 and 1,
    so a correct "pichhle 90 din mein ₹8,900" fails the guard twice and the merchant gets no spoken answer."""
    today = app_today(db)
    frm = str(today - timedelta(days=days - 1))

    def steps():
        return [
            lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
            lambda m, model: [tool_use("get_payer_summary", {
                "payer_id": last_tool_result(m)["matches"][0]["payer_id"], "from": frm, "to": str(today)}, 2)],
            lambda m, model: [final(
                display=template.format(total=int(last_tool_result(m)["total_rupees"]), n=last_tool_result(m)["payments"]),
                speech=template.format(total=int(last_tool_result(m)["total_rupees"]), n=last_tool_result(m)["payments"]),
                components=[{"type": "payer_card", "result_id": "r2"}])],
        ]
    fake = fake_claude(steps() + steps())
    out = await ask(api, demo_token, question)
    assert not out["fallback"] and out["speech"], (
        f"grounded answer rejected after {len(fake.calls)} model calls: {out['display']!r}")


async def test_guard_accepts_the_true_moment1_answer(api, demo_token, fake_claude, db):
    """The grounded moment-1 answer (₹12,100, 121, ₹100, 8:30, 4 mahine, aaj) must pass the guard on the first model."""
    today = app_today(db)
    fake = fake_claude(summary_steps(
        "Ram", str(_months_back(today, 4)), str(today),
        "Ram ne pichhle 4 mahine mein ₹12,100 diye: 121 payments, lagbhag hamesha ₹100 subah 8:30 ke aas-paas. "
        "Aakhri payment aaj.",
        "राम ने पिछले 4 महीने में ₹12,100 दिए, 121 payments, ज़्यादातर ₹100 सुबह 8:30 के आसपास।"))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert not out["fallback"] and out["speech"] and len(fake.calls) == 3
    card = next(c for c in out["components"] if c["type"] == "payer_card")["data"]
    assert card["payments"] == 121 and card["total_paise"] == 12_100_00 and card["last_payment_was"] == "today"


# ---------------------------------------------------------------------------
# Moment 1: "the payments behind it" whatever component the model names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("components", [
    [],  # the model forgot to attach anything
    [{"type": "table", "result_id": "r2"}],  # a type that isn't allowed for get_payer_summary
    [{"type": "payer_card", "result_id": "r9"}],  # a result id that doesn't exist
])
async def test_moment1_always_shows_the_data_behind_the_answer(api, demo_token, fake_claude, db, components):
    """Moment 1: answered "with a spoken reply plus the payments behind it". The server fills components, but if the
    model's pick is empty or invalid, the grounded answer is shown with nothing behind it (only the find_payer-chips
    safety net exists)."""
    today = app_today(db)
    fake_claude(summary_steps("Ram", str(_months_back(today, 4)), str(today), "Ram ne ₹12,100 diye, 121 payments.",
                              "राम ने ₹12,100 दिए, 121 payments।", components))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert not out["fallback"]
    assert any(c["type"] in ("payer_card", "table", "stat_cards") for c in out["components"]), (
        f"answer {out['display']!r} shown with components {[c['type'] for c in out['components']]}")


# ---------------------------------------------------------------------------
# Names: honorifics are part of how merchants name customers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Ram ji", "Ram bhaiya", "Ram bhai", "राम जी", "Mr Ram"])
async def test_honorific_still_finds_the_customer(name):
    """Risks: names are fuzzy-searched. "Sharma ji" and "Meena ben" resolve to their customer, but "Ram ji" /
    "Ram bhaiya" come back as no_exact_match, which marks Ram as a lookalike for the rest of the question."""
    from app.db import connection, fetch_one
    from app.services.agent.tools import ToolContext, run_tool

    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        ctx = ToolContext(conn=conn, merchant_id=m["id"])
        out, _ = await run_tool(ctx, "find_payer", {"name": name})
    assert out["single_match"] and out["matches"][0]["name"] == "Ram", out


async def test_moment1_with_an_honorific_answers_for_ram(api, demo_token, fake_claude, db):
    """"Ram bhaiya ne pichhle 4 mahine mein kitna diya?" -- if the model passes the name as said, the payer summary is
    refused ("only looks similar") and the merchant gets chips instead of the answer."""
    today = app_today(db)
    fake_claude(summary_steps("Ram bhaiya", str(_months_back(today, 4)), str(today),
                              "Ram ne ₹12,100 diye, 121 payments.", "राम ने ₹12,100 दिए, 121 payments।"))
    out = await ask(api, demo_token, "Ram bhaiya ne pichhle 4 mahine mein kitna diya?")
    types = [c["type"] for c in out["components"]]
    assert "payer_card" in types, f"got {types}: {out['display']!r}"


async def test_unknown_name_cannot_be_answered_via_a_second_search(api, demo_token, fake_claude, db):
    """Round-8 fix re-check: "Ramesh" -> no_exact_match; a model that then searches "Ram" and asks for the summary is
    refused, and the merchant sees chips, not Ram's ₹12,100."""
    today = app_today(db)
    fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Ramesh"})],
        lambda m, model: [tool_use("find_payer", {"name": "Ram"}, 2)],
        lambda m, model: [tool_use("get_payer_summary", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                         "from": str(_months_back(today, 4)), "to": str(today)}, 3)],
        lambda m, model: [final(display="Ramesh naam ka koi customer nahi mila. Kya aap Ram ki baat kar rahe hain?",
                                speech="", components=[])],
    ])
    out = await ask(api, demo_token, "Ramesh ne pichhle 4 mahine mein kitna diya?")
    assert "12,100" not in out["display"]
    assert [c["type"] for c in out["components"]] == ["chips"]


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("q", ["Ram total batana", "Sharma Traders reminder bhejna", "Ram payment details dikhana",
                               "Ram ka hisaab", "Aaj collection kitna hua?"])
def test_hinglish_without_postpositions_is_still_hinglish(q):
    """UI copy "Hindi · English · Hinglish": a Hinglish question gets a Hinglish reply. The language check only knows
    a word list, so imperatives like batana / bhejna / dikhana make the question "English" and force an English reply."""
    assert reply_language(q) == "hi"


@pytest.mark.parametrize("q", ["How much did Ram pay in the last 4 months?", "Show Ram's payments since July",
                               "Who paid the most this week?", "Send Sharma Traders a reminder"])
def test_english_questions_stay_english(q):
    assert reply_language(q) == "en"


async def test_language_note_reaches_the_model(api, demo_token, fake_claude, db):
    today = app_today(db)
    fake = fake_claude(summary_steps("Ram", str(_months_back(today, 4)), str(today), "Ram paid you ₹12,100.",
                                     "Ram paid you ₹12,100."))
    await ask(api, demo_token, "How much did Ram pay in the last 4 months?")
    turn = next(m["content"] for m in fake.calls[0]["messages"] if isinstance(m["content"], str))
    assert "Answer language: English" in turn and str(_months_back(today, 4)) in turn


# ---------------------------------------------------------------------------
# Dates: "pichhle mahine" as a calendar month
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("today,month,week", [
    (date(2026, 10, 31), "2026-09-01 to 2026-09-30", "2026-10-19 to 2026-10-25"),
    (date(2027, 1, 5), "2026-12-01 to 2026-12-31", "2026-12-28 to 2027-01-03"),
    (date(2028, 3, 1), "2028-02-01 to 2028-02-29", "2028-02-21 to 2028-02-27"),
])
def test_previous_month_and_week_are_calendar_ranges(today, month, week):
    line = range_starts(today)
    assert f"Previous calendar month (pichhle mahine): {month}" in line
    assert f"Previous week (pichhle hafte): {week}" in line


async def test_pichhle_mahine_answer_uses_september(api, demo_token, fake_claude, db):
    """Stub follows the given range: Ram's September is 29 payments / ₹2,900 (server data, not the model's)."""
    fake = fake_claude(summary_steps("Ram", "2026-09-01", "2026-09-30", "Ram ne pichhle mahine ₹2,900 diye, 29 payments.",
                                     "राम ने पिछले महीने ₹2,900 दिए, 29 payments।"))
    if app_today(db) != date(2026, 10, 31):
        pytest.skip("seeded clock is not 31 Oct")
    out = await ask(api, demo_token, "Ram ne pichhle mahine kitna diya?")
    turn = next(m["content"] for m in fake.calls[0]["messages"] if isinstance(m["content"], str))
    assert "2026-09-01 to 2026-09-30" in turn
    card = out["components"][0]["data"]
    assert (card["payments"], card["total_paise"], out["fallback"]) == (29, 2_900_00, False)


# ---------------------------------------------------------------------------
# Charts on longer ranges
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("months,grain", [(3, "week"), (3, "month"), (2, "week")])
async def test_sales_chart_covers_the_same_window_as_the_totals_on_long_ranges(db, months, grain):
    """get_sales_summary -> line_chart + stat_cards. Round 8 fixed ranges up to 31 days; "pichhle 3 mahine" (31 Jul to
    31 Oct) by week starts the chart on the next Monday and by month on 1 Aug, so the chart leaves days out that the
    "Collected" card counts."""
    from app.db import connection, fetch_one
    from app.services.agent.components import build_components
    from app.services.agent.tools import ToolContext, run_tool

    today = app_today(db)
    frm = _months_back(today, months)
    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        ctx = ToolContext(conn=conn, merchant_id=m["id"])
        out, _ = await run_tool(ctx, "get_sales_summary", {"from": str(frm), "to": str(today), "grain": grain})
        comps = build_components([{"type": "line_chart", "result_id": out["result_id"]},
                                  {"type": "stat_cards", "result_id": out["result_id"]}], ctx)
    chart_total = sum(p["total_paise"] for p in comps[0]["data"]["points"])
    card_total = comps[1]["data"]["items"][0]["value_paise"]
    first = comps[0]["data"]["points"][0]["bucket"]
    assert chart_total == card_total, (
        f"{frm}..{today} by {grain}: chart starts {first}, ₹{chart_total / 100:,.0f} vs card ₹{card_total / 100:,.0f}")


# ---------------------------------------------------------------------------
# Retry paths
# ---------------------------------------------------------------------------
async def test_the_sonnet_retry_uses_a_tool_choice_sonnet_accepts(api, demo_token, fake_claude, db):
    """Voice path 6: "retry once on Sonnet". Live, claude-sonnet-5-5 answers every first turn with 400
    'tool_choice: type "tool" and "any" are not supported for this model.' (reproduced directly: any -> 400, auto ->
    tool_use), so the retry never runs. The fake model can't reject it, so this checks what the runner sends."""
    today = app_today(db)
    bad = summary_steps("Ram", str(_months_back(today, 4)), str(today), "Ram ne ₹99,999 diye.", "राम ने ₹99,999 दिए।")
    fake = fake_claude(bad + bad)
    await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    sonnet = [c for c in fake.calls if c["model"] == "claude-sonnet-5-5"]
    assert sonnet, "no Sonnet retry after a guard failure"
    assert sonnet[0]["tool_choice"] != {"type": "any"}, "Sonnet rejects tool_choice any with HTTP 400"


async def test_guard_fallback_after_a_failed_retry_still_shows_the_data(api, demo_token, fake_claude, db):
    """Voice path 6: "...then fall back to showing the data with no speech". When the Sonnet retry errors (live: always,
    see above), the fallback is built from Sonnet's empty tool context: the merchant gets "Maaf kijiye, iska pakka jawab
    nahi de paaya" and nothing else, although Haiku had found Ram's payments."""
    today = app_today(db)
    bad = summary_steps("Ram", str(_months_back(today, 4)), str(today), "Ram ne ₹99,999 diye.", "राम ने ₹99,999 दिए।")
    err = anthropic.BadRequestError(
        "tool_choice: type \"tool\" and \"any\" are not supported for this model.",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)
    fake_claude(bad + [raises(err)])
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert out["fallback"] is True and out["speech"] is None
    assert any(c["type"] == "payer_card" for c in out["components"]), (
        f"fallback shows {out['display']!r} with components {[c['type'] for c in out['components']]}")
async def test_english_language_miss_then_sonnet_outage_keeps_the_answers_data(api, demo_token, fake_claude, db):
    """Haiku answers an English question in Hinglish (language miss -> retry on Sonnet); Sonnet is down. The Haiku answer
    is kept, but its components and follow-ups are rebuilt against Sonnet's empty tool context, so the payer card
    disappears."""
    today = app_today(db)
    steps = summary_steps("Ram", str(_months_back(today, 4)), str(today),
                          "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments.",
                          "राम ने ₹12,100 दिए।")
    conn_err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    fake_claude(steps + [raises(conn_err)])
    out = await ask(api, demo_token, "How much did Ram pay in the last 4 months?")
    assert "12,100" in out["display"]
    assert any(c["type"] == "payer_card" for c in out["components"]), (
        f"display kept but components {[c['type'] for c in out['components']]}")


async def test_pending_action_survives_language_miss_then_outage(api, other_merchant, fake_claude, db):
    """Same path for a write: the kept answer says the invoice is ready to confirm, so the confirm card must be there."""
    due = str(app_today(db) + timedelta(days=5))
    steps = [
        lambda m, model: [tool_use("find_payer", {"name": "Asha Test"})],
        lambda m, model: [tool_use("create_invoice", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                      "amount": 400, "due_date": due, "items": [], "email": None}, 2)],
        lambda m, model: [final(display="Asha Test ka ₹400 ka invoice ready hai, confirm karein.",
                                speech="Asha Test का ₹400 का invoice ready है।",
                                components=[{"type": "confirm_action", "result_id": "r2"}])],
    ]
    conn_err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    fake_claude(steps + [raises(conn_err)])
    out = await ask(api, other_merchant["token"], "Create a 400 rupee invoice for Asha Test due in 5 days")
    assert out["pending_action"] and any(c["type"] == "confirm_action" for c in out["components"]), (
        f"display {out['display']!r} with no confirm card")
    n = db.execute("select count(*) as n from invoices where merchant_id = %s", (other_merchant["merchant_id"],)).fetchone()
    assert n["n"] == 0


# ---------------------------------------------------------------------------
# Confirm-before-write re-checks (throwaway merchant)
# ---------------------------------------------------------------------------
async def test_recurring_card_names_the_email_that_will_be_used(api, other_merchant, fake_claude, db):
    """Round-8 fix re-check: the recurring confirm card says where invoices go, and confirming uses that address."""
    fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Asha Test"})],
        lambda m, model: [tool_use("set_recurring", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                     "cadence": "monthly", "amount": 500, "email": None}, 2)],
        lambda m, model: [final(display="Asha Test ko monthly ₹500 recurring mark karne ke liye confirm karein.",
                                speech="", components=[{"type": "confirm_action", "result_id": "r2"}])],
    ])
    out = await ask(api, other_merchant["token"], "Asha Test ko monthly 500 ka recurring mark karo")
    summary = out["pending_action"]["summary"]
    assert summary["email"] == "asha@example.com" and "asha@example.com" in summary["delivery"]
    assert db.execute("select count(*) as n from recurring_schedules where merchant_id = %s",
                      (other_merchant["merchant_id"],)).fetchone()["n"] == 0
    r = await api.post(f"/actions/{out['pending_action']['token']}/confirm", headers=bearer(other_merchant["token"]))
    assert r.status_code == 200, r.text
    row = db.execute("select email, active from recurring_schedules where merchant_id = %s",
                     (other_merchant["merchant_id"],)).fetchone()
    assert row == {"email": "asha@example.com", "active": True}


# ---------------------------------------------------------------------------
# Request shape the web client really sends
# ---------------------------------------------------------------------------
async def test_a_long_earlier_question_in_history_does_not_break_the_next_question(api, demo_token, fake_claude, db):
    """The panel resends the last 3 questions as history. A chip pick re-asks "<question> (<name>)" and a voice
    transcript can be long; a history question over 500 characters makes every next typed question a 422 until it
    scrolls out of the last 3 turns."""
    today = app_today(db)
    fake_claude(summary_steps("Ram", str(today), str(today), "Ram ne aaj ₹100 diye.", "राम ने आज ₹100 दिए।"))
    long_q = "Ram ne " + "pichhle kuch mahino mein " * 20 + "kitna diya? (Ram)"
    assert 500 < len(long_q) < 600
    r = await api.post("/ask", json={"text": "Ram ne aaj kitna diya?",
                                     "history": [{"question": long_q, "answer": "Ram ne ₹12,100 diye."}]},
                       headers=bearer(demo_token))
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
