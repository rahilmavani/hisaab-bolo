"""Tester round 10, M6 "Ask Hisaab": re-checks of the round-9 fixes plus new edge cases, with the model stubbed.

Spec (docs/ARCHITECTURE.md):
  - Moment 1: "Ram ne pichhle 4 mahine mein kitna diya?" is answered with a spoken reply plus the payments behind it.
  - Voice path 6: "Number guard: every number in `speech` must appear in a tool result; otherwise retry once on
    Sonnet, then fall back to showing the data with no speech."
  - Dynamic UI: "The model picks a type and a tool-result ID, and the server fills the rows."
  - Agent: "Write actions create a pending action that needs a confirm tap. The model never writes."
  - Languages: Hindi, English, Hinglish (UI header "Hindi · English · Hinglish").
  - Risks: "Speech-to-text mishears names: trigram search plus disambiguation chips."

Seeded facts: Ram pays ₹100 daily around 8:30; 121 payments / ₹12,100 since 1 Jul; October = 31 payments / ₹3,100.
Writes use the throwaway `other_merchant`; the demo merchant is only read.
"""

from __future__ import annotations

from datetime import date, timedelta

import anthropic
import httpx
import pytest

from app.services.agent.runner import (
    _months_back,
    question_numbers,
    range_starts,
    reply_language,
)

from ..conftest import bearer
from .test_r9_m6_agent import FakeClaude, final, last_tool_result, raises, tool_use


@pytest.fixture
def fake_claude(monkeypatch):
    def install(steps):
        fake = FakeClaude(steps)
        monkeypatch.setattr("app.services.agent.runner.client", lambda: fake)
        return fake
    return install


def app_today(db) -> date:
    return db.execute("select app_today() as d").fetchone()["d"]


def summary_steps(name, frm, to, display, speech, components=None, extra_summary=False):
    """find_payer(name) -> get_payer_summary(first match) [-> the same summary again] -> final answer."""
    steps = [
        lambda m, model: [tool_use("find_payer", {"name": name})],
        lambda m, model: [tool_use("get_payer_summary", {
            "payer_id": last_tool_result(m)["matches"][0]["payer_id"], "from": frm, "to": to}, 2)],
    ]
    if extra_summary:  # live, Sonnet looked the same window up twice before answering
        steps.append(lambda m, model: [tool_use("get_payer_summary", {
            "payer_id": last_tool_result(m)["payer_id"], "from": frm, "to": to}, 3)])
    steps.append(lambda m, model: [final(display=display, speech=speech, lang="hi-IN",
                                         components=components if components is not None
                                         else [{"type": "payer_card", "result_id": "r2"}])])
    return steps


async def ask(api, token, text, **body) -> dict:
    r = await api.post("/ask", json={"text": text, **body}, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Moment 1 in standard Hindi: plural time words
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("question,expected", [
    ("राम ने पिछले चार महीनों में कितना दिया?", "4"),
    ("राम ने पिछले 4 महीनों में कितना दिया?", "4"),
    ("राम ने पिछले 30 दिनों में कितना दिया?", "30"),
    ("पिछले दो हफ़्तों की सेल कितनी हुई?", "2"),
    ("पिछले 3 हफ्तों में कितना आया?", "3"),
])
def test_devanagari_plural_time_words_count_as_time_quantities(question, expected):
    """question_numbers() lets "4 mahine / 4 mahinon / चार महीने" be repeated back, but not the Devanagari oblique plural
    (महीनों, दिनों, हफ़्तों) that standard Hindi uses after "पिछले चार" and that Saaras writes when it is said."""
    assert expected in question_numbers(question).split(), f"{question!r} -> {question_numbers(question)!r}"


async def test_moment1_in_standard_hindi_gets_its_spoken_reply(api, demo_token, fake_claude, db):
    """Live (2 of 2, typed and by voice): "राम ने पिछले चार महीनों में कितना दिया?" -> Haiku and Sonnet both answer
    "पिछले 4 महीने में ₹12,100", the guard rejects "4" both times, and the merchant gets "Yeh raha jo mila." with no
    speech after ~12 s. The grounded answer must pass."""
    today = app_today(db)
    display = "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments."
    speech = "राम ने पिछले 4 महीनों में ₹12,100 दिए, 121 payments।"
    steps = summary_steps("Ram", str(_months_back(today, 4)), str(today), display, speech)
    fake = fake_claude(steps + summary_steps("Ram", str(_months_back(today, 4)), str(today), display, speech))
    out = await ask(api, demo_token, "राम ने पिछले चार महीनों में कितना दिया?")
    assert not out["fallback"] and out["speech"], (
        f"correct moment-1 answer fell back after {len(fake.calls)} model calls: {out['display']!r}")


# ---------------------------------------------------------------------------
# Fallback: "show the data with no speech" without asking which customer
# ---------------------------------------------------------------------------
async def test_fallback_after_two_guard_failures_shows_one_card_and_no_which_customer_chips(api, demo_token,
                                                                                         fake_claude, db):
    """Live: the fallback for the moment-1 question showed chips "Ram · Vikram Agarwal" (role group "Which customer?")
    above two identical payer cards: the auto components take every result of the busiest attempt, including the
    find_payer search that had already settled on Ram, and a repeated summary."""
    today = app_today(db)
    frm, to = str(_months_back(today, 4)), str(today)
    bad = ("Ram ne ₹99,999 diye.", "राम ने ₹99,999 दिए।")
    fake_claude(summary_steps("Ram", frm, to, *bad) + summary_steps("Ram", frm, to, *bad, extra_summary=True))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert out["fallback"] is True and out["speech"] is None
    types = [c["type"] for c in out["components"]]
    assert types.count("payer_card") == 1 and "chips" not in types, f"fallback components: {types}"


# ---------------------------------------------------------------------------
# Moment 1: "the payments behind it" when the model names the search result
# ---------------------------------------------------------------------------
async def test_payer_card_named_on_the_search_result_still_shows_the_payments(api, demo_token, fake_claude, db):
    """The model answers "₹12,100, 121 payments" but names payer_card with r1 (the find_payer result) instead of r2.
    build_components turns that into chips, so the backup view never runs and the answer is shown with a lone "Ram"
    chip instead of the card with the payments behind it."""
    today = app_today(db)
    fake_claude(summary_steps("Ram", str(_months_back(today, 4)), str(today),
                              "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments.",
                              "राम ने पिछले 4 महीने में ₹12,100 दिए, 121 payments।",
                              components=[{"type": "payer_card", "result_id": "r1"}]))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    types = [c["type"] for c in out["components"]]
    assert not out["fallback"] and any(t in ("payer_card", "table", "stat_cards") for t in types), (
        f"answer {out['display']!r} shown with {types}")


# ---------------------------------------------------------------------------
# Number guard: wrong amounts that still pass, right ones that don't
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("display,speech", [
    # 121 is the truth; "एक सौ बाईस" is 122. सौ (100) is grounded by "₹100", बाईस isn't a known number word.
    ("Ram ne pichhle 4 mahine mein ek sau baais payments kiye.", "राम ने पिछले 4 महीने में एक सौ बाईस payments किए।"),
    # ₹100 a day is the truth; "do sau" is ₹200. "do" is skipped as ambiguous and "sau" (100) is grounded.
    ("Ram roz do sau rupaye deta hai.", "राम रोज़ दो सौ रुपये देता है।"),
])
async def test_guard_catches_wrong_amounts_said_as_compound_hindi_words(api, demo_token, fake_claude, db, display,
                                                                        speech):
    today = app_today(db)
    frm = str(_months_back(today, 4))
    fake_claude(summary_steps("Ram", frm, str(today), display, speech)
                + summary_steps("Ram", frm, str(today), display, speech))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert out["fallback"] is True and out["speech"] is None, f"wrong amount passed the guard: {out['speech']!r}"


async def test_an_amount_said_before_a_month_name_is_not_evidence(api, demo_token, fake_claude, db):
    """Round 9 fix: an amount the merchant states must come from a tool result. TIME_QUANTITY_RE takes any number in
    front of a month name as a time quantity, so "3000 October" grounds ₹3,000 and the model may confirm it (Ram's
    October is ₹3,100)."""
    today = app_today(db)
    oct1 = today.replace(day=1)
    display = "Haan, Ram ne October mein ₹3,000 diye."
    fake_claude(summary_steps("Ram", str(oct1), str(today), display, "हाँ, राम ने October में ₹3,000 दिए।")
                + summary_steps("Ram", str(oct1), str(today), display, "हाँ, राम ने October में ₹3,000 दिए।"))
    out = await ask(api, demo_token, "Kya Ram ne 3000 October mein diye?")
    assert out["fallback"] is True and out["speech"] is None, f"echoed the merchant's amount: {out['display']!r}"


async def test_a_correct_time_written_with_a_dot_passes_the_guard(api, demo_token, fake_claude, db):
    """Indian English often writes 8:30 as "8.30". The grounded answer "subah 8.30 baje ₹100" is rejected (8.3 is not a
    tool number), costing a Sonnet retry and, if Sonnet writes it the same way, the spoken reply."""
    today = app_today(db)
    frm = str(_months_back(today, 4))
    ok = ("Ram roz subah 8.30 baje ₹100 deta hai.", "राम रोज़ सुबह 8.30 बजे ₹100 देता है।")
    fake = fake_claude(summary_steps("Ram", frm, str(today), *ok) + summary_steps("Ram", frm, str(today), *ok))
    out = await ask(api, demo_token, "Ram kab aur kitna deta hai?")
    assert not out["fallback"] and out["speech"], f"grounded time rejected after {len(fake.calls)} calls"


@pytest.mark.parametrize("question", ["पिछले नब्बे दिन में कितनी सेल हुई?", "pichhle nabbe din ki sale"])
def test_round_day_counts_said_as_words_are_grounded(question):
    """Round-9 fix re-check: a round number from the question is repeatable, also when said as a word."""
    assert "90" in question_numbers(question).split()


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Ramji", "रामजी"])
async def test_honorific_written_together_with_the_name_finds_the_customer(name):
    """Round-9 fix covers "Ram ji" / "राम जी". Speech-to-text and merchants also write it joined ("Ramji", "रामजी");
    find_payer then reports no_exact_match and marks Ram a lookalike, so the question can't be answered about Ram."""
    from app.db import connection, fetch_one
    from app.services.agent.tools import ToolContext, run_tool

    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        ctx = ToolContext(conn=conn, merchant_id=m["id"])
        out, _ = await run_tool(ctx, "find_payer", {"name": name})
    assert out["single_match"] and out["matches"][0]["name"] == "Ram", out


@pytest.mark.parametrize("name", ["Ram ji", "राम जी", "Ram bhaiya", "Sharma ji", "शर्मा जी"])
async def test_spaced_honorifics_still_resolve(name):
    """Round-9 fix re-check."""
    from app.db import connection, fetch_one
    from app.services.agent.tools import ToolContext, run_tool

    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        ctx = ToolContext(conn=conn, merchant_id=m["id"])
        out, _ = await run_tool(ctx, "find_payer", {"name": name})
    assert out["single_match"], out


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("q", ["Hisaab, how much did Ram pay last month?", "Hey Hisaab, show me today's sales"])
def test_addressing_the_assistant_by_name_keeps_an_english_question_english(q):
    """The panel is called "Ask Hisaab", and "hisaab" is a Hinglish marker: an English question that starts by
    addressing the assistant gets a Hinglish reply."""
    assert reply_language(q) == "en"


@pytest.mark.parametrize("q", ["Aaj ki kamai kitni hui?", "Kal ka collection batao", "Top customer kaun hai?",
                               "Sharma ka bill bana do", "Pichhle 3 mahine ki sale"])
def test_common_hinglish_questions_get_hinglish(q):
    """Round-9 fix re-check."""
    assert reply_language(q) == "hi"


# ---------------------------------------------------------------------------
# Dates (round-9 fix re-checks)
# ---------------------------------------------------------------------------
def test_last_30_days_starts_29_days_back():
    today = date(2026, 10, 31)
    line = range_starts(today)
    assert "last 30 days: 2026-10-02" in line and "last 90 days: 2026-08-03" in line
    assert "this week: 2026-10-26" in line and "last 4 months: 2026-06-30" in line


@pytest.mark.parametrize("months,grain", [(3, "month"), (3, "week"), (4, "day")])
async def test_long_range_chart_still_sums_to_the_collected_card(db, months, grain):
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
    assert sum(p["total_paise"] for p in comps[0]["data"]["points"]) == comps[1]["data"]["items"][0]["value_paise"]
    assert comps[0]["data"]["points"][0]["bucket"] == str(frm)


# ---------------------------------------------------------------------------
# Retry paths (round-9 fix re-checks)
# ---------------------------------------------------------------------------
async def test_a_primary_model_that_rejects_a_forced_tool_is_retried_with_auto(api, demo_token, fake_claude, db,
                                                                              monkeypatch):
    from app.services.agent import runner

    monkeypatch.setattr(runner, "_NO_FORCED_TOOL", set())
    today = app_today(db)
    err = anthropic.BadRequestError(
        "tool_choice: type \"tool\" and \"any\" are not supported for this model.",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)
    good = summary_steps("Ram", str(_months_back(today, 4)), str(today), "Ram ne ₹12,100 diye, 121 payments.",
                         "राम ने ₹12,100 दिए, 121 payments।")
    fake = fake_claude([raises(err)] + good)
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert not out["fallback"] and out["speech"] and out["model"] == "claude-haiku-5-5"
    assert fake.calls[1]["tool_choice"] == {"type": "auto"}


async def test_sonnet_outage_after_guard_failure_still_shows_haikus_data(api, demo_token, fake_claude, db):
    today = app_today(db)
    bad = summary_steps("Ram", str(_months_back(today, 4)), str(today), "Ram ne ₹99,999 diye.", "राम ने ₹99,999 दिए।")
    conn_err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    fake_claude(bad + [raises(conn_err)])
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert out["fallback"] and out["speech"] is None
    card = next(c for c in out["components"] if c["type"] == "payer_card")["data"]
    assert (card["payments"], card["total_paise"]) == (121, 12_100_00)


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------
async def test_a_blank_question_is_refused_without_a_paid_model_call(api, demo_token, fake_claude):
    """AskBody only checks min_length on the raw text, so "   " passes and the runner asks Claude an empty question."""
    fake = fake_claude([lambda m, model: [final(display="Kya poochna chahte hain?", speech="")]] * 2)
    r = await api.post("/ask", json={"text": "   "}, headers=bearer(demo_token))
    assert r.status_code == 422 and fake.calls == [], (r.status_code, len(fake.calls))


# ---------------------------------------------------------------------------
# Confirm-before-write (throwaway merchant)
# ---------------------------------------------------------------------------
async def test_invoice_by_chat_writes_nothing_until_confirm_and_only_for_its_merchant(api, other_merchant, demo_token,
                                                                                   fake_claude, db):
    due = str(app_today(db) + timedelta(days=7))
    fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Asha Test"})],
        lambda m, model: [tool_use("create_invoice", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                      "amount": 750, "due_date": due, "items": ["Atta"],
                                                      "email": None}, 2)],
        lambda m, model: [final(display="Asha Test ka ₹750 ka invoice ready hai. Confirm karein.",
                                speech="Asha Test का ₹750 का invoice ready है।",
                                components=[{"type": "confirm_action", "result_id": "r2"}])],
    ])
    out = await ask(api, other_merchant["token"], "Asha Test ka 750 ka invoice banao, 7 din mein due")
    token = out["pending_action"]["token"]
    count = "select count(*) as n from invoices where merchant_id = %s"
    assert db.execute(count, (other_merchant["merchant_id"],)).fetchone()["n"] == 0
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(demo_token))  # another merchant's token
    assert r.status_code == 404
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(other_merchant["token"]))
    assert r.status_code == 200, r.text
    assert db.execute(count, (other_merchant["merchant_id"],)).fetchone()["n"] == 1
    r = await api.post(f"/actions/{token}/confirm", headers=bearer(other_merchant["token"]))
    assert r.status_code == 409
    assert db.execute(count, (other_merchant["merchant_id"],)).fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# Moment 1's own follow-up
# ---------------------------------------------------------------------------
async def test_the_follow_up_after_moment1_answers_on_the_first_model(api, demo_token, fake_claude, db):
    """Live (1 of 1): after moment 1 the model offered "Ram ke sabhi payments dikhao"; tapping it, Haiku answered
    "pichhle 4 mahine ke saare 121 payments" and the guard rejected "4" (the period comes from the conversation, not
    this question), so Sonnet ran too: 16 s for the deck's "Show all 121 payments" step."""
    today = app_today(db)
    frm = str(_months_back(today, 4))
    fake = fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use("list_transactions", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                         "from": frm, "to": str(today), "limit": 200}, 2)],
        lambda m, model: [final(display="Ram ke pichhle 4 mahine ke saare 121 payments neeche hain, kul ₹12,100.",
                                speech="राम के पिछले 4 महीने के सारे 121 payments नीचे हैं।",
                                components=[{"type": "table", "result_id": "r2"}])],
    ] * 2)
    history = [{"question": "Ram ne pichhle 4 mahine mein kitna diya?",
                "answer": "Ram ne pichhle 4 mahine mein kul ₹12,100 diye, 121 payments mein, roz ₹100."}]
    out = await ask(api, demo_token, "Ram ke sabhi payments dikhao", history=history)
    assert len(fake.calls) == 3 and not out["fallback"] and out["model"] == "claude-haiku-5-5", (
        f"{len(fake.calls)} model calls, model {out['model']}, fallback {out['fallback']}")
