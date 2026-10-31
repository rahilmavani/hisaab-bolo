"""Tester round 11, M6 "Ask Hisaab": re-checks of the round-10 fixes plus new edge cases, with the model stubbed.

Spec (docs/ARCHITECTURE.md):
  - Moment 1: "Ram ne pichhle 4 mahine mein kitna diya?" is answered with a spoken reply plus the payments behind it.
  - Voice path 6: "Number guard: every number in `speech` must appear in a tool result; otherwise retry once on
    Sonnet, then fall back to showing the data with no speech."
  - Dynamic UI: "The model picks a type and a tool-result ID, and the server fills the rows."
  - Agent: "Write actions create a pending action that needs a confirm tap. The model never writes."
  - Languages: Hindi, English, Hinglish (UI header "Hindi · English · Hinglish").
  - Risks: "Speech-to-text mishears names: trigram search plus disambiguation chips."

Seeded facts: Ram pays ₹100 daily around 8:30; 121 payments / ₹12,100 in the last 4 months.
Writes use the throwaway `other_merchant`; the demo merchant is only read.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from app.services.agent.runner import _months_back, question_numbers, reply_language

from ..conftest import bearer
from .test_r9_m6_agent import FakeClaude, final, last_tool_result, tool_use
from .test_r10_m6_agent import app_today, ask, summary_steps

HAIKU = "claude-haiku-5-5"


@pytest.fixture
def fake_claude(monkeypatch):
    def install(steps):
        fake = FakeClaude(steps)
        monkeypatch.setattr("app.services.agent.runner.client", lambda: fake)
        return fake
    return install


def moment1(db, display, speech, components=None, name="Ram", twice=True):
    today = app_today(db)
    one = summary_steps(name, str(_months_back(today, 4)), str(today), display, speech, components)
    return one + summary_steps(name, str(_months_back(today, 4)), str(today), display, speech, components) if twice \
        else one


async def find(name: str) -> dict:
    from app.db import connection, fetch_one
    from app.services.agent.tools import ToolContext, run_tool

    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        out, _ = await run_tool(ToolContext(conn=conn, merchant_id=m["id"]), "find_payer", {"name": name})
    return out


# ===========================================================================
# Round-10 fixes, re-checked
# ===========================================================================
@pytest.mark.parametrize("question,expected", [
    ("राम ने पिछले चार महीनों में कितना दिया?", "4"),
    ("राम ने पिछले ४ महीनों में कितना दिया?", "4"),
    ("पिछले 30 दिनों में कितनी सेल हुई?", "30"),
    ("पिछले दो हफ़्तों में कितना आया?", "2"),
    ("Ram ne pichhle chaar mahino mein kitna diya?", "4"),
])
def test_r10_fix_devanagari_and_roman_plural_time_words(question, expected):
    assert expected in question_numbers(question).split(), question_numbers(question)


async def test_r10_fix_moment1_with_mahinon_is_answered_by_the_first_model(api, demo_token, fake_claude, db):
    display = "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments."
    speech = "राम ने पिछले 4 महीनों में ₹12,100 दिए, 121 payments।"
    fake = fake_claude(moment1(db, display, speech))
    out = await ask(api, demo_token, "राम ने पिछले चार महीनों में कितना दिया?")
    assert not out["fallback"] and out["speech"] == speech and out["model"] == HAIKU and len(fake.calls) == 3
    card = next(c for c in out["components"] if c["type"] == "payer_card")["data"]
    assert (card["payments"], card["total_paise"]) == (121, 12_100_00)


async def test_r10_fix_fallback_shows_one_card_and_no_settled_search_chips(api, demo_token, fake_claude, db):
    today = app_today(db)
    frm, to = str(_months_back(today, 4)), str(today)
    bad = ("Ram ne ₹99,999 diye.", "राम ने ₹99,999 दिए।")
    fake_claude(summary_steps("Ram", frm, to, *bad, extra_summary=True)
                + summary_steps("Ram", frm, to, *bad, extra_summary=True))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert out["fallback"] is True and out["speech"] is None
    assert [c["type"] for c in out["components"]] == ["payer_card"], [c["type"] for c in out["components"]]


async def test_r10_fix_follow_up_may_repeat_the_earlier_devanagari_range(api, demo_token, fake_claude, db):
    """History is the Devanagari moment-1 question ("चार महीनों"); the follow-up repeats "4 महीने"."""
    today = app_today(db)
    frm = str(_months_back(today, 4))
    fake = fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use("list_transactions", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                         "from": frm, "to": str(today), "limit": 200}, 2)],
        lambda m, model: [final(display="Ram ke pichhle 4 mahine ke saare 121 payments neeche hain.",
                                speech="राम के पिछले 4 महीनों के सारे 121 payments नीचे हैं।",
                                components=[{"type": "table", "result_id": "r2"}])],
    ] * 2)
    history = [{"question": "राम ने पिछले चार महीनों में कितना दिया?",
                "answer": "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments."}]
    out = await ask(api, demo_token, "Ram ke saare payments dikhao", history=history)
    assert not out["fallback"] and len(fake.calls) == 3, (out["fallback"], len(fake.calls))
    table = next(c for c in out["components"] if c["type"] == "table")["data"]
    assert table["total_rows"] == 121 and len(table["rows"]) == 121


async def test_r10_fix_payer_card_named_on_the_search_result_shows_the_summary(api, demo_token, fake_claude, db):
    fake_claude(moment1(db, "Ram ne pichhle 4 mahine mein ₹12,100 diye, 121 payments.",
                        "राम ने पिछले 4 महीने में ₹12,100 दिए, 121 payments।",
                        components=[{"type": "payer_card", "result_id": "r1"}], twice=False))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert [c["type"] for c in out["components"]] == ["payer_card"]
    assert out["components"][0]["data"]["payments"] == 121


@pytest.mark.parametrize("display,speech", [
    ("Ram ne pichhle 4 mahine mein ek sau baais payments kiye.", "राम ने पिछले 4 महीने में एक सौ बाईस payments किए।"),
    ("Ram roz do sau rupaye deta hai.", "राम रोज़ दो सौ रुपये देता है।"),
])
async def test_r10_fix_compound_number_words_are_checked_as_one_number(api, demo_token, fake_claude, db, display,
                                                                        speech):
    fake_claude(moment1(db, display, speech))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert out["fallback"] is True and out["speech"] is None


async def test_r10_fix_time_written_with_a_dot_passes(api, demo_token, fake_claude, db):
    fake = fake_claude(moment1(db, "Ram roz subah 8.30 baje ₹100 deta hai.", "राम रोज़ सुबह 8.30 बजे ₹100 देता है।"))
    out = await ask(api, demo_token, "Ram kab aur kitna deta hai?")
    assert not out["fallback"] and out["speech"] and len(fake.calls) == 3


@pytest.mark.parametrize("name,expected", [("Ramji", "Ram"), ("रामजी", "Ram"), ("RAMJI", "Ram"),
                                           ("Sharmaji", "Sharma Traders"), ("शर्माजी", "Sharma Traders")])
async def test_r10_fix_joined_ji_finds_the_customer(name, expected):
    out = await find(name)
    assert out["single_match"] and out["matches"][0]["name"] == expected, out


@pytest.mark.parametrize("q,lang", [("Hisaab, how much did Ram pay in the last 4 months?", "en"),
                                    ("Hey Hisaab, what were today's sales?", "en"),
                                    ("Hisaab, Ram ne kitna diya?", "hi")])
def test_r10_fix_addressing_hisaab_keeps_the_question_language(q, lang):
    assert reply_language(q) == lang


@pytest.mark.parametrize("text", ["   ", "\n\t "])
async def test_r10_fix_blank_question_is_refused_without_a_model_call(api, demo_token, fake_claude, text):
    fake = fake_claude([])
    r = await api.post("/ask", json={"text": text}, headers=bearer(demo_token))
    assert r.status_code == 422 and fake.calls == []


@pytest.mark.parametrize("months,grain", [(3, "week"), (3, "month"), (4, "month"), (6, "month"), (6, "week")])
async def test_r10_fix_long_range_chart_has_no_sliver_first_point(db, months, grain):
    from app.db import connection, fetch_one
    from app.services.agent.components import build_components
    from app.services.agent.tools import ToolContext, run_tool

    today = app_today(db)
    frm = _months_back(today, months)
    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        ctx = ToolContext(conn=conn, merchant_id=m["id"])
        out, _ = await run_tool(ctx, "get_sales_summary", {"from": str(frm), "to": str(today), "grain": grain})
        chart, cards = build_components([{"type": "line_chart", "result_id": out["result_id"]},
                                         {"type": "stat_cards", "result_id": out["result_id"]}], ctx)
    pts = chart["data"]["points"]
    assert sum(p["total_paise"] for p in pts) == cards["data"]["items"][0]["value_paise"]
    assert pts[0]["bucket"] == str(frm)
    if len(pts) > 1:  # the first point covers at least a few days, not a 1-2 day sliver
        from datetime import date
        span = (date.fromisoformat(pts[1]["bucket"]) - date.fromisoformat(pts[0]["bucket"])).days
        assert span >= (3 if grain == "week" else 10), pts[:2]


# ===========================================================================
# New in round 11
# ===========================================================================
PRECOMPOSED_HAZAAR = "हज़ार"  # "हज़ार" typed with the single code point ज़ (U+095B)


async def test_guard_catches_a_wrong_amount_written_with_a_precomposed_nukta(api, demo_token, fake_claude, db):
    """"तेरह हज़ार" (₹13,000; the truth is ₹12,100). The guard knows हज़ार only as ज + ़; written with the single code
    point ज़ (U+095B, as many keyboards and some model outputs do), हज़ार is not a number word, so only "तेरह" (13) is
    checked, and 13 is grounded by any date like 2026-10-13 in the tool result."""
    speech = f"राम ने पिछले 4 महीनों में तेरह {PRECOMPOSED_HAZAAR} रुपये दिए।"
    fake_claude(moment1(db, "Ram ne pichhle 4 mahine mein ₹12,100 diye.", speech))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert out["fallback"] is True and out["speech"] is None, f"wrong amount passed: {out['speech']!r}"


async def test_guard_catches_an_english_count_with_and(api, demo_token, fake_claude, db):
    """"one hundred and thirty-one payments" is 131 (the truth is 121). "and" and the hyphen split it into 100, 30
    and a lone "one" (skipped as ambiguous); 100 is grounded by ₹100 and 30 by the window start 2026-06-30."""
    text = "Ram made one hundred and thirty-one payments in the last 4 months, ₹12,100 in all."
    fake_claude(moment1(db, text, text))
    out = await ask(api, demo_token, "How much did Ram pay in the last 4 months?")
    assert out["fallback"] is True and out["speech"] is None, f"wrong count passed: {out['display']!r}"


async def test_guard_catches_a_wrong_count_in_roman_hinglish_words_on_screen(api, demo_token, fake_claude, db):
    """display is checked too, but Roman Hinglish 21-99 ("baais", "ikkis") are not number words, so
    "ek sau baais" reads as 100 (grounded). Speech here is correct; the screen says 122."""
    fake_claude(moment1(db, "Ram ne pichhle 4 mahine mein ek sau baais payments kiye, kul ₹12,100.",
                        "राम ने पिछले 4 महीने में 121 payments किए, कुल ₹12,100।"))
    out = await ask(api, demo_token, "Ram ne pichhle 4 mahine mein kitna diya?")
    assert out["fallback"] is True, f"wrong count shown on screen: {out['display']!r}"


@pytest.mark.parametrize("name,expected", [("Rambhai", "Ram"), ("रामभाई", "Ram"), ("Meenaben", "Meena Kapoor")])
async def test_joined_bhai_and_ben_find_the_customer(name, expected):
    """HONORIFICS has bhai and ben ("Ram bhai", "Meena ben" work), and round 10 fixed joined "ji". The Gujarati joined
    forms "Rambhai", "Meenaben" still miss: Rambhai -> no_exact_match (Ram then off limits), Meenaben -> nobody."""
    out = await find(name)
    assert out["single_match"] and out["matches"][0]["name"] == expected, out


@pytest.mark.parametrize("question", ["राम ने पिछले चार महिने में कितना दिया?", "राम ने पिछले चार महीनो में कितना दिया?",
                                      "Ram ne pichhle 4 maheene mein kitna diya?"])
def test_common_spellings_of_months_count_as_time_words(question):
    """महिने (short i) and महीनो (no anusvara) are common typed spellings, "maheene" a common Roman one. Without them
    the "4" of a correct moment-1 answer is ungrounded (the round-10 महीनों failure: both models, no speech)."""
    assert "4" in question_numbers(question).split(), question_numbers(question)


async def test_payments_table_note_never_promises_more_rows_than_the_table_has(db):
    """list_transactions tells the model "the merchant's table lists all N payments (with a Show all button)" whenever
    it returns more than 20 rows, even when `limit` cut the rows below N; the table then offers "Show all 50 of 121"
    while the answer can say all 121 are below."""
    from app.db import connection, fetch_one
    from app.services.agent.components import build_components
    from app.services.agent.tools import ToolContext, run_tool

    today = app_today(db)
    async with connection() as conn:
        m = await fetch_one(conn, "select id from merchants where name = 'Gupta Kirana Store'")
        ctx = ToolContext(conn=conn, merchant_id=m["id"])
        found, _ = await run_tool(ctx, "find_payer", {"name": "Ram"})
        out, _ = await run_tool(ctx, "list_transactions", {"payer_id": found["matches"][0]["payer_id"],
                                                           "from": str(_months_back(today, 4)), "to": str(today),
                                                           "limit": 50})
        (table,) = build_components([{"type": "table", "result_id": out["result_id"]}], ctx)
    promised = re.search(r"lists all (\d+)", out.get("note") or "")
    rows = len(table["data"]["rows"])
    assert not promised or int(promised.group(1)) == rows, f"note: {out['note']!r}; table rows {rows}"


@pytest.mark.parametrize("text", ["?", "।", "..."])
async def test_punctuation_only_question_is_refused_like_a_voice_one(api, demo_token, fake_claude, text):
    """/voice/ask refuses a transcript with no word characters; /ask sends "?" or "।" to the paid model."""
    fake = fake_claude([lambda m, model: [final(display="Kya poochna hai?", speech="")]] * 4)
    r = await api.post("/ask", json={"text": text}, headers=bearer(demo_token))
    assert r.status_code == 422 and fake.calls == [], (r.status_code, len(fake.calls))


async def test_two_prepared_actions_each_need_their_own_confirm(api, other_merchant, fake_claude, db):
    due = str(app_today(db) + timedelta(days=7))
    fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Asha Test"})],
        lambda m, model: [
            tool_use("create_invoice", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"], "amount": 750,
                                        "due_date": due, "items": ["Atta"], "email": None}, 2),
        ],
        lambda m, model: [tool_use("set_recurring", {"payer_id": str(other_merchant["payer_a"]), "cadence": "monthly",
                                                     "amount": 750, "email": None}, 3)],
        lambda m, model: [final(display="Invoice aur monthly ₹750 dono ready hain. Confirm karein.",
                                speech="Invoice और monthly ₹750 दोनों ready हैं।",
                                components=[{"type": "confirm_action", "result_id": "r2"},
                                            {"type": "confirm_action", "result_id": "r3"}])],
    ])
    out = await ask(api, other_merchant["token"], "Asha Test ka 750 ka invoice banao aur har mahine 750 recurring karo")
    mid = other_merchant["merchant_id"]
    q = {"inv": "select count(*) as n from invoices where merchant_id = %s",
         "rec": "select count(*) as n from recurring_schedules where merchant_id = %s"}
    assert [c["type"] for c in out["components"]].count("confirm_action") == 2
    assert all(db.execute(s, (mid,)).fetchone()["n"] == 0 for s in q.values())
    first = out["components"][0]["data"]["token"]
    r = await api.post(f"/actions/{first}/confirm", headers=bearer(other_merchant["token"]))
    assert r.status_code == 200, r.text
    assert db.execute(q["inv"], (mid,)).fetchone()["n"] == 1 and db.execute(q["rec"], (mid,)).fetchone()["n"] == 0


async def test_an_expired_prepared_action_does_nothing(api, other_merchant, db):
    from psycopg.types.json import Jsonb

    db.execute("insert into pending_actions (token, merchant_id, tool, args, summary, expires_at) values "
               "('pa_r11_expired', %s, 'create_invoice', %s, %s, app_now() - interval '1 minute')",
               (other_merchant["merchant_id"],
                Jsonb({"payer_id": str(other_merchant["payer_a"]), "amount_paise": 75000,
                       "due_date": str(app_today(db) + timedelta(days=7)),
                       "line_items": [{"name": "Atta", "qty": 1, "unit_price_paise": 75000}], "email": None}),
                Jsonb({"title": "Invoice for Asha Test"})))
    r = await api.post("/actions/pa_r11_expired/confirm", headers=bearer(other_merchant["token"]))
    assert r.status_code == 410
    n = db.execute("select count(*) as n from invoices where merchant_id = %s",
                   (other_merchant["merchant_id"],)).fetchone()["n"]
    assert n == 0


async def test_english_moment1_is_spoken_in_english(api, demo_token, fake_claude, db):
    text = "Ram paid ₹12,100 in the last 4 months, across 121 payments."
    today = app_today(db)
    fake = fake_claude([
        lambda m, model: [tool_use("find_payer", {"name": "Ram"})],
        lambda m, model: [tool_use("get_payer_summary", {"payer_id": last_tool_result(m)["matches"][0]["payer_id"],
                                                         "from": str(_months_back(today, 4)), "to": str(today)}, 2)],
        lambda m, model: [final(display=text, speech=text, lang="en-IN",
                                components=[{"type": "payer_card", "result_id": "r2"}])],
    ])
    out = await ask(api, demo_token, "Hisaab, how much did Ram pay in the last four months?")
    assert not out["fallback"] and out["lang"] == "en-IN" and out["speech"] == text and len(fake.calls) == 3
    assert "Answer language: English" in fake.calls[0]["messages"][0]["content"]
