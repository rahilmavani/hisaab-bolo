"""Ask Hisaab: Claude tool loop → {speech, display, components[], pending_action?}.

- At most 4 model turns. System prompt and tools are prompt-cached; today's date
  (app clock, IST) rides in the user turn so the cached prefix stays stable.
- Number guard: every number in `speech` must appear in a tool result (or the
  question / today's date). Otherwise retry once on the fallback model, then show
  the data with no speech.
- The model picks a component type + result_id; the server fills the rows.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

import anthropic
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from ...config import get_settings
from ...db import app_now, fetch_one
from ...money import IST
from .components import build_components
from .tools import TOOLS, WRITE_TOOLS, ToolContext, run_tool

log = logging.getLogger(__name__)
MAX_TURNS = 4
USD_TO_PAISE = 8800  # ₹88 per USD, for the cost log
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}  # $/MTok in, out

COMPONENT_TYPES = ["stat_cards", "line_chart", "table", "payer_card", "invoice_card", "confirm_action", "chips"]

SYSTEM_PROMPT = """You are Hisaab, the voice ledger assistant inside Hisaab Bolo, used by small Indian shopkeepers who \
take payments on Paytm. The merchant asks about their own money in Hindi, English or Hinglish, usually by voice \
(the question is a speech-to-text transcript, so customer names may be misspelled or written in Devanagari).

How to work:
- Use the tools for every fact. Never guess an amount, count, date or name. All amounts in tool results are rupees.
- Customer questions: call find_payer first (it is fuzzy), passing the name in Roman letters (राम → Ram, \
शर्मा ट्रेडर्स → Sharma Traders). If several customers plausibly match and nothing in the question decides it, \
ask which one and show the matches as chips; do not pick one yourself. If find_payer reports no_exact_match, no \
customer has that name: say so, show the similar names as chips, and say nothing about their payments. A bare \
customer name as the whole question means: summarise that customer's payments for the last 30 days.
- Dates: tool date ranges are inclusive IST calendar dates. "Pichhle 4 mahine" / "last 4 months" means from the same \
date 4 months ago through today: take the start from the range starts given with each question. "Aaj" = today, \
"kal" = yesterday (when asking about the past), "is hafte" = this week from Monday, "is mahine" = this month from \
the 1st, "pichhle mahine" / "last month" (no number) = the previous calendar month, "pichhle hafte" / "last week" \
= the previous Monday-Sunday week, "pichhle N din" / "last N days" = the N days ending today (today counts). \
Today's date is given with each question.
- Write actions (create_invoice, send_reminder, set_recurring, call_customer) only prepare a pending action. Nothing happens until \
the merchant taps Confirm, so say it is ready to confirm, never that it is done. Include a confirm_action component.
- Pick components that show the data behind your answer, referencing result_id values from tool results: \
payer_card for one customer's summary, table for lists of payments or invoices, line_chart and stat_cards for sales, \
invoice_card for an invoice, confirm_action for a pending action, chips to choose between customers. Only mention \
something "below" if you attach that component. A table lists every row the tool counted (with a "Show all" \
button), even when the tool result shows you only the first rows.

How to answer (the final message is JSON matching the schema):
- display: what appears on screen, 1-3 short sentences. English question → English. Hindi or Hinglish question \
(in any script) → Hinglish in Roman letters. A question in another Indian language (Gujarati, Marathi, Tamil, \
…) → that language in its own script. Lead with the answer.
- speech: the same answer as display, in the same language, for text-to-speech, at most 2 short sentences. For an \
English question speak plain English. For a Hindi or Hinglish question, always \
write the Hindi words in Devanagari (even if the question was typed in Roman letters) and keep English words such \
as payment, invoice, reminder and customer names in Roman letters. Write every number as digits (₹12,100, 121, 8:30) \
and only use numbers that appear in tool results or in the question. Leave speech empty if you have nothing \
grounded to say.
- lang: "hi-IN" if speech is Hindi or Hinglish, the language's code (gu-IN, mr-IN, ta-IN, …) for another Indian \
language, otherwise "en-IN".
- follow_ups: up to 2 short next questions the merchant might tap (e.g. "Show all 121 payments", "Compare with last \
year"), in the display language.
- If the question is not about this shop's payments, customers, invoices or reminders, say briefly what you can help \
with and use no components."""

# Indian languages besides Hindi that Sarvam hears and speaks (Bulbul v3), by its language code.
OTHER_LANGUAGES = {"gu": "Gujarati", "mr": "Marathi", "bn": "Bengali", "pa": "Punjabi", "od": "Odia", "ta": "Tamil",
                   "te": "Telugu", "kn": "Kannada", "ml": "Malayalam"}
# Unicode block of each script (Marathi writes in Devanagari, so only the voice path's detected language tells it
# apart from Hindi).
SCRIPT_BLOCKS = {"bn": 0x0980, "pa": 0x0A00, "gu": 0x0A80, "od": 0x0B00, "ta": 0x0B80, "te": 0x0C00, "kn": 0x0C80,
                 "ml": 0x0D00}
# Every Indic script keeps its digits at the same place in its block; the number guard reads Western digits.
INDIC_DIGITS = {base + 0x66 + i: str(i) for base in range(0x0900, 0x0D80, 0x80) for i in range(10)}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "display": {"type": "string"},
        "speech": {"type": "string"},
        "lang": {"type": "string", "enum": ["hi-IN", "en-IN", *(f"{c}-IN" for c in OTHER_LANGUAGES)]},
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": COMPONENT_TYPES}, "result_id": {"type": "string"}},
                "required": ["type", "result_id"],
                "additionalProperties": False,
            },
        },
        "follow_ups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["display", "speech", "lang", "components", "follow_ups"],
    "additionalProperties": False,
}

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        # A hung call should fail over to the fallback model in seconds, not after 40 s x 3 tries.
        _client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key or None, max_retries=1, timeout=20)
    return _client


def _cached_tools() -> list[dict]:
    tools = [dict(t) for t in TOOLS]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


# ---------------------------------------------------------------------------
# Number guard
# ---------------------------------------------------------------------------
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


# "8.30 baje" is a time, like "8:30" in the tool results, not the decimal 8.3.
TIME_DOT_RE = re.compile(r"\b(\d{1,2})\.(\d{2})(?=\s*(?:baje|am|pm|a\.m|p\.m|बजे))", re.I)


def _plain(text: str) -> str:
    return TIME_DOT_RE.sub(r"\1:\2", text.translate(DEVANAGARI_DIGITS))


# Spelling-tolerant Devanagari for matching words: no nukta (precomposed ज़ or ज + ़), no nasal marks, short i
# read as long i. So महिने = महीने = महीनो, हज़ार = हजार, पाँच = पांच.
NUKTA_LETTERS = str.maketrans({"\u0958": "क", "\u0959": "ख", "\u095A": "ग", "\u095B": "ज", "\u095C": "ड",
                               "\u095D": "ढ", "\u095E": "फ", "\u095F": "य"})


def _norm_dev(text: str) -> str:
    return re.sub("[\u0901\u0902\u093C]", "", text.translate(NUKTA_LETTERS)).replace("\u093F", "\u0940")


def numbers_in(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in NUM_RE.findall(_plain(text)):
        try:
            out.add(Decimal(m.replace(",", "").rstrip(".")).normalize())
        except InvalidOperation:
            continue
    return out


# Saaras transcribes spoken numbers as words ("चार महीने"); count them as grounded by the question.
NUMBER_WORDS = {
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "सात": 7, "आठ": 8, "नौ": 9,
    "दस": 10, "ग्यारह": 11, "बारह": 12, "पंद्रह": 15, "बीस": 20, "तीस": 30, "सौ": 100, "हज़ार": 1000, "हजार": 1000,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6, "saat": 7,
    "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12, "baarah": 12, "pandrah": 15, "bees": 20, "tees": 30,
    "sau": 100, "hazaar": 1000, "hazar": 1000,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "hundred": 100, "thousand": 1000,
    "तेरह": 13, "चौदह": 14, "सोलह": 16, "सत्रह": 17, "अठारह": 18, "उन्नीस": 19, "पच्चीस": 25, "चालीस": 40,
    "पचास": 50, "साठ": 60, "सत्तर": 70, "अस्सी": 80, "नब्बे": 90, "लाख": 100000, "करोड़": 10000000,
    "करोड": 10000000, "डेढ़": 1.5, "ढाई": 2.5,
    "terah": 13, "chaudah": 14, "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19, "pachees": 25, "chalis": 40,
    "pachaas": 50, "pachas": 50, "sattar": 70, "assi": 80, "nabbe": 90, "lakh": 100000, "lac": 100000,
    "crore": 10000000, "karod": 10000000, "dedh": 1.5, "dhai": 2.5,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "million": 1000000,
}
# Hindi has a distinct word for every number up to 100 ("बाईस" = 22), so list 21-99 too.
NUMBER_WORDS.update({w: n for n, w in enumerate(
    "इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस "
    "सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास "
    "पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ "
    "सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी "
    "इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे "
    "पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे".split(), start=21)})
# The same in Roman letters, as the on-screen Hinglish might spell them (common variants).
for _n, _spellings in {
    21: "ikkees ikkis", 22: "baais bais", 23: "teis teyis", 24: "chaubees chaubis", 25: "pacchees pachchis",
    26: "chhabbees chhabbis", 27: "sattaees sattais", 28: "atthaees atthais", 29: "untees untis",
    31: "ikattees iktis", 32: "battees battis", 33: "taintees taintis", 34: "chautees chautis",
    35: "paintees paintis", 36: "chhattees chhattis", 37: "saintees saintis", 38: "adtees adtis",
    39: "untaalees untalis", 41: "iktaalees iktalis", 42: "bayaalees bayalis", 43: "taintaalees",
    44: "chavaalees chavalis", 45: "paintaalees paintalis", 46: "chhiyaalees", 47: "saintaalees",
    48: "adtaalees adtalis", 49: "unchaas", 51: "ikyaavan ikyavan", 52: "baavan bavan", 53: "tirpan",
    54: "chauvan", 55: "pachpan", 56: "chhappan", 57: "sattaavan", 58: "atthaavan", 59: "unsath",
    61: "iksath", 62: "baasath", 63: "tirsath", 64: "chaunsath", 65: "painsath", 66: "chhiyaasath",
    67: "sadsath", 68: "adsath", 69: "unhattar", 71: "ikhattar", 72: "bahattar", 73: "tihattar",
    74: "chauhattar", 75: "pachhattar", 76: "chhihattar", 77: "sathattar", 78: "athhattar", 79: "unaasi",
    81: "ikyaasi", 82: "bayaasi", 83: "tiraasi", 84: "chauraasi", 85: "pachaasi", 86: "chhiyaasi",
    87: "sattaasi", 88: "atthaasi", 89: "navaasi", 91: "ikyaanve", 92: "baanve", 93: "tiraanve",
    94: "chauraanve", 95: "panchaanve", 96: "chhiyaanve", 97: "sattaanve", 98: "atthaanve", 99: "ninyaanve",
    60: "saath sath", 50: "pachaas",
}.items():
    NUMBER_WORDS.update(dict.fromkeys(_spellings.split(), _n))
NUMBER_WORDS_NORM = {_norm_dev(w): n for w, n in NUMBER_WORDS.items()}
WORD_RE = re.compile(r"[\w\u0900-\u097F]+")


# Standing alone in an answer these are usually not numbers ("एक payment", "dikha do", "the one"); next to
# another number word they are ("do sau" = 200).
AMBIGUOUS_NUMBER_WORDS = {"एक", "दो", "ek", "do", "one", "che", "saath", "sath"}  # saath: "with"
TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?|[\w\u0900-\u097F]+")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# A number in the question counts as grounded only as a time quantity ("4 mahine", "30 din", "8 baje",
# "5 October"); an amount the merchant states ("Ram ne aaj 125 diye kya?") must come from a tool result.
TIME_UNIT = (r"(din|dino|dinon|mahine|mahina|mahino|mahinon|hafte|hafta|hafton|saal|sal|days?|months?|weeks?|"
             r"years?|baje|am|pm|ghante|ghanta|hours?|tarikh|tareekh|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|"
             r"jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*|maheene|maheena|mahiney|mahinay|"
             # Devanagari in _norm_dev form (matched against a normalised question): दिनों, महिने, हफ़्ते, घंटे ...
             r"दीनो|दीन|महीनो|महीने|महीना|माह|हफ्तो|हफ्ते|हफ्ता|सालो|साल|बजे|घटो|घटे|घटा|तारीखो|तारीख)")
TIME_QUANTITY_RE = re.compile(r"([\d०-९]+|[\w\u0900-\u097F]+)\s*" + TIME_UNIT + r"(?![\w\u0900-\u097F])", re.I)
DAY_OF_MONTH_UNIT = re.compile(r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|tarikh|tareekh|तारीख", re.I)
CLOCK_UNIT = re.compile(r"baje|am|pm|बजे", re.I)
# Gujarati and Marathi time words take suffixes ("4 મહિનામાં", "3 दिवसांत"), so their stems match.
OTHER_TIME_RE = re.compile(r"(\d+)\s*(?:મહિન|દિવસ|અઠવાડ|વર્ષ|વાગ્|કલાક|તારીખ|महिन|महीन|दिवस|आठवड|वर्ष|वाज|तास|तारीख)")


def number_words(text: str) -> set[Decimal]:
    words = WORD_RE.findall(_norm_dev(text).lower())
    return {Decimal(str(NUMBER_WORDS_NORM[w])) for w in words if w in NUMBER_WORDS_NORM}


def _token_value(tok: str) -> Decimal | None:
    if tok[0].isdigit():
        try:
            return Decimal(tok.replace(",", "").rstrip(".")).normalize()
        except InvalidOperation:
            return None
    return Decimal(str(NUMBER_WORDS_NORM[tok])) if tok in NUMBER_WORDS_NORM else None


def _compound(values: list[Decimal]) -> Decimal:
    """Spoken number to value: "बारह हज़ार एक सौ" = 12,100; "12 हज़ार" = 12,000; "डेढ़ लाख" = 1,50,000."""
    total, current = Decimal(0), Decimal(0)
    for v in values:
        if v == 100:
            current = (current or 1) * v
        elif v >= 1000:
            total += (current or 1) * v
            current = Decimal(0)
        else:
            current += v
    return (total + current).normalize()


def answer_numbers(text: str) -> set[Decimal]:
    """Every number an answer states, as digits or as words (the model sometimes writes "छह payments").
    Number words next to each other are one number, so "एक सौ बाईस" is checked as 122, not as 100 and 22."""
    plain = _norm_dev(_plain(text)).lower()
    plain = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", plain)  # "thirty-one"
    plain = re.sub(r"\b(hundred|thousand|lakh|lac|crore|million|sau|hazaar|hazar)\s+and\s+", r"\1 ", plain)
    out: set[Decimal] = set()
    group: list[tuple[str, Decimal]] = []
    last_end = 0

    def flush():
        words = [t for t, _ in group if not t[0].isdigit()]
        if len(group) > 1 and any(NUMBER_WORDS_NORM.get(w, 0) >= 100 for w in words):
            out.add(_compound([v for _, v in group]))
        else:
            out.update(v for t, v in group if t[0].isdigit() or t not in AMBIGUOUS_NUMBER_WORDS)
        group.clear()

    for m in TOKEN_RE.finditer(plain):
        value = _token_value(m.group(0))
        if value is None:
            flush()
            continue
        if group and plain[last_end:m.start()].strip():
            flush()
        group.append((m.group(0), value))
        last_end = m.end()
    flush()
    return out


def question_numbers(question: str) -> str:
    """Numbers from the question that may be repeated back: time quantities only (see TIME_UNIT), and only
    plausible ones ("3000 October" is an amount, not a date)."""
    nums: set[Decimal] = set()
    for qty, unit in TIME_QUANTITY_RE.findall(_norm_dev(question)):
        cap = 31 if DAY_OF_MONTH_UNIT.match(unit) else 24 if CLOCK_UNIT.fullmatch(unit) else 1000
        nums |= {n for n in numbers_in(qty) | number_words(qty) if n <= cap}
    nums |= {Decimal(q) for q in OTHER_TIME_RE.findall(question.translate(INDIC_DIGITS)) if int(q) <= 1000}
    return " ".join(format(n, "f") for n in nums)  # plain digits: str(Decimal("3E+1")) reads back as 3 and 1


def grounded_numbers(sources: list) -> set[Decimal]:
    allowed: set[Decimal] = set()
    for s in sources:
        text = s if isinstance(s, str) else json.dumps(s, default=str, ensure_ascii=False)
        allowed |= numbers_in(UUID_RE.sub(" ", text))  # digit runs inside ids aren't facts
    # 8:30 → 8 and 30 are already split by the regex; a bare clock hour like "8 baje" also passes via 8.
    return allowed


def visible_sources(ctx: ToolContext) -> list:
    """What the model actually saw: tool results without hidden (_) fields, plus pending summaries."""
    return ([{k: v for k, v in r["data"].items() if not k.startswith("_")} for r in ctx.results.values()]
            + [p["summary"] for p in ctx.pending])


def ungrounded(text: str, allowed: set[Decimal]) -> set[Decimal]:
    return {n for n in answer_numbers(text) if n not in allowed}


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------
# Words that mark a Roman-script question as Hinglish (English questions rarely contain them).
HINGLISH_MARKERS = {
    "ne", "ka", "ki", "ke", "kitna", "kitne", "kitni", "hai", "hain", "karo", "kar", "mein", "aaj", "kal", "hafte",
    "mahine", "mahina", "bhej", "bhejo", "dikhao", "bana", "banao", "batao", "kaun", "kiska", "kiske", "kisne", "raha",
    "rahi", "diya", "diye", "wala", "aur", "pichhle", "pichle", "nahi", "kya", "kab", "kaisa", "kaise", "rupaye", "rupay",
    "se", "ko", "mila", "mile", "mili", "aaya", "aayi", "aaye", "aya", "paisa", "paise", "baki", "baaki", "bacha",
    "abhi", "sab", "saare", "saari", "liya", "liye", "udhaar", "udhar", "hua", "hui", "hue", "tha", "thi", "kiya",
    "bata", "batana", "dikha", "dikhana", "bhejna", "karna", "chahiye", "kul", "sirf", "mujhe", "unka", "uska", "tak",
    "din", "saal", "baje",  # not "hisaab": that's the assistant's name ("Hisaab, how much did Ram pay?")
}


def hinglish_words(text: str) -> int:
    return len(set(re.findall(r"[a-z]+", text.lower())) & HINGLISH_MARKERS)


def reply_language(question: str, heard: str | None = None) -> str:
    """"en", "hi" or an OTHER_LANGUAGES code: answer in the language asked. heard is the speech-to-text language."""
    for code, base in SCRIPT_BLOCKS.items():
        if any(base <= ord(ch) < base + 0x80 for ch in question):
            return code
    if any("\u0900" <= ch <= "\u097f" for ch in question):
        return "mr" if heard == "mr-IN" else "hi"
    words = set(re.findall(r"[a-z]+", question.lower()))
    return "hi" if words & HINGLISH_MARKERS else "en"


FALLBACK_TEXT = {
    "en": ("Here is what I found.",
           "Sorry, I couldn't answer that reliably. Try asking about a customer, a date range or your invoices."),
    "hi": ("Yeh raha jo mila.",
           "Maaf kijiye, iska pakka jawab nahi de paaya. Kisi customer, tareekh ya invoice ke baare mein poochiye."),
    "gu": ("આ રહ્યું જે મળ્યું.",
           "માફ કરશો, આનો પાકો જવાબ ન આપી શક્યો. કોઈ customer, તારીખ કે invoice વિશે પૂછો."),
    "mr": ("हे मिळालं.",
           "माफ करा, याचं नक्की उत्तर देता आलं नाही. एखाद्या customer, तारखेबद्दल किंवा invoice बद्दल विचारा."),
}

LANGUAGE_NOTE = {
    "en": "Answer language: English. display and speech in plain English (no Hindi or Hinglish words, even when "
          "confirming an action), lang en-IN.",
    "hi": "Answer language: Hinglish. display in Roman-script Hinglish; speech in Devanagari script with English "
          "words and names in Roman letters; lang hi-IN.",
    **{code: f"Answer language: {name}. display, speech and follow_ups in {name} script, with English words such as "
             f"payment, invoice, reminder and customer names in Roman letters and numbers in Western digits "
             f"(₹12,100, 121); lang {code}-IN." for code, name in OTHER_LANGUAGES.items()},
}


def _context_line(now, merchant_name: str) -> str:
    local = now.astimezone(IST)
    return (f"Today is {local.strftime('%A, %d %B %Y')}, {local.strftime('%I:%M %p').lstrip('0')} IST. "
            f"Shop: {merchant_name}.")


def _months_back(today: date, n: int) -> date:
    """The same date n months ago, clamped to that month's end (31 Oct - 4 months = 30 Jun)."""
    y, m = divmod(today.year * 12 + today.month - 1 - n, 12)
    return date(y, m + 1, min(today.day, calendar.monthrange(y, m + 1)[1]))


def range_starts(today: date) -> str:
    """Precomputed starts for relative ranges, so "pichhle 4 mahine" never depends on the model's
    calendar arithmetic (it once answered from 31 Jul, three months back)."""
    parts = [f"last {n} days: {today - timedelta(days=n - 1)}" for n in (7, 14, 30, 60, 90)]
    parts += [f"this week: {today - timedelta(days=today.weekday())}", f"this month: {today.replace(day=1)}"]
    parts += [f"last {n} month{'s' if n > 1 else ''}: {_months_back(today, n)}" for n in (1, 2, 3, 4, 6, 12)]
    month_end = today.replace(day=1) - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday() + 7)
    return ("Range starts (each runs through today): " + "; ".join(parts) + ". Previous calendar month "
            f"(pichhle mahine): {month_end.replace(day=1)} to {month_end}. Previous week (pichhle hafte): "
            f"{week_start} to {week_start + timedelta(days=6)}.")


async def _loop(ctx: ToolContext, model: str, messages: list[dict],
                force_first_tool: bool = False) -> tuple[dict | None, dict]:
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "turns": 0}
    msgs = list(messages)
    nudged = False
    for turn in range(MAX_TURNS):
        last = turn == MAX_TURNS - 1
        # The first turn must look something up: left to choose, Haiku sometimes (2 in 8 live tries) ended
        # at once with a progress note ("Pehle Ram ka record dhoondhte hain") and no answer. Some models
        # refuse a forced tool (Sonnet 5.5 answers 400), so only the primary model is forced.
        force = force_first_tool and turn == 0 and model not in _NO_FORCED_TOOL
        tool_choice = {"type": "none"} if last else {"type": "any"} if force else {"type": "auto"}
        try:
            resp = await _create(model, msgs, tool_choice)
        except anthropic.BadRequestError as e:
            if not force or "tool_choice" not in str(e):
                raise
            _NO_FORCED_TOOL.add(model)
            resp = await _create(model, msgs, {"type": "auto"})
        usage["turns"] += 1
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        usage["cache_read"] += resp.usage.cache_read_input_tokens or 0
        usage["cache_write"] += resp.usage.cache_creation_input_tokens or 0
        if resp.stop_reason == "refusal":
            return None, usage
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason == "tool_use" and tool_uses:
            msgs.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                out, is_error = await run_tool(ctx, tu.name, dict(tu.input))
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": json.dumps(out, default=str, ensure_ascii=False), "is_error": is_error})
            msgs.append({"role": "user", "content": results})
            continue
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            log.warning("agent returned non-JSON: %r", text[:200])
            return None, usage
        if not last and not nudged and _stopped_halfway(ctx, answer):
            # Seen live: "Ram ka data dhoondh raha hoon..." as the final answer, right after finding Ram.
            nudged = True
            msgs.append({"role": "assistant", "content": resp.content})
            msgs.append({"role": "user", "content": "That was only a progress note. You have the customer: use the "
                                                    "tools now and answer the merchant's question in the final message."})
            continue
        return answer, usage
    return None, usage


_NO_FORCED_TOOL: set[str] = set()  # models that rejected tool_choice "any"


async def _create(model: str, msgs: list[dict], tool_choice: dict):
    return await client().messages.create(
        model=model,
        max_tokens=4000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=_cached_tools(),
        tool_choice=tool_choice,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        messages=msgs,
    )


def _stopped_halfway(ctx: ToolContext, answer: dict) -> bool:
    """The model found exactly one customer and then ended with nothing to show for it."""
    tools = [r["tool"] for r in ctx.results.values()]
    if not tools or set(tools) != {"find_payer"} or answer.get("components"):
        return False
    return bool(list(ctx.results.values())[-1]["data"].get("single_match"))


def _cost_paise(model: str, usage: dict) -> int:
    pin, pout = PRICES.get(model, (2.0, 10.0))
    usd = (usage["input"] * pin + usage["cache_read"] * pin * 0.1 + usage["cache_write"] * pin * 1.25
           + usage["output"] * pout) / 1_000_000
    return max(1, round(usd * USD_TO_PAISE)) if usd else 0


async def ask(
    conn: AsyncConnection,
    merchant_id: UUID,
    merchant_name: str,
    question: str,
    *,
    history: list[dict] | None = None,
    payer_hint: str | None = None,
    source: str = "text",
    heard_lang: str | None = None,
) -> dict:
    s = get_settings()
    started = time.monotonic()
    now = await app_now(conn)
    context = _context_line(now, merchant_name)
    hint = ""
    if payer_hint:
        p = await fetch_one(conn, "select id, display_name from payers where id = %s and merchant_id = %s",
                            (payer_hint, merchant_id))
        if p:
            hint = f"\nThe merchant picked this customer: {p['display_name']} (payer_id {p['id']})."
    messages: list[dict] = []
    for h in (history or [])[-3:]:
        if h.get("question") and h.get("answer"):
            messages += [{"role": "user", "content": h["question"]}, {"role": "assistant", "content": h["answer"]}]
    origin = "Voice transcript" if source == "voice" else "Typed question"
    q_lang = reply_language(question, heard_lang)
    lang_note = LANGUAGE_NOTE[q_lang]
    ranges = range_starts(now.astimezone(IST).date())
    messages.append({"role": "user", "content": f"{context} {ranges}{hint}\n{lang_note}\n{origin}: {question}"})

    # The range starts stay out of the guard's sources: a date the answer uses is in the tool result.
    # Follow-ups ("saari payments dikhao") may repeat the time range of the question they continue.
    earlier = " ".join(question_numbers(h.get("question", "")) for h in (history or [])[-3:])
    allowed_extra = [question_numbers(question), earlier, context, hint]
    attempts = []
    contexts: list[ToolContext] = []
    kept: tuple[dict, ToolContext] | None = None  # the answer to show, with the lookups behind it
    for model in (s.agent_model, s.agent_fallback_model):
        ctx = ToolContext(conn=conn, merchant_id=merchant_id, picked=str(payer_hint) if payer_hint else None)
        contexts.append(ctx)
        try:
            answer, usage = await _loop(ctx, model, messages, force_first_tool=model == s.agent_model)
        except anthropic.APIError as e:
            log.error("agent call failed on %s: %s", model, e)
            attempts.append({"model": model, "error": str(e)})
            continue
        attempts.append({"model": model, "usage": usage, "cost_paise": _cost_paise(model, usage)})
        if answer is None:
            continue
        answer["display"], answer["speech"] = (answer[k].translate(INDIC_DIGITS) for k in ("display", "speech"))
        answer["follow_ups"] = [f.translate(INDIC_DIGITS) for f in answer.get("follow_ups", [])]
        allowed = grounded_numbers(visible_sources(ctx) + allowed_extra)
        guard_failed = ungrounded(answer["speech"], allowed) | ungrounded(answer["display"], allowed)
        if guard_failed:
            log.warning("number guard failed on %s: %s", model, sorted(map(str, guard_failed)))
            attempts[-1]["guard_failed"] = sorted(map(str, guard_failed))
            continue
        kept = (answer, ctx)
        if q_lang == "en" and hinglish_words(answer["display"]) >= 2 and model != s.agent_fallback_model:
            # English question, Hinglish answer: the next model gets a try; this one stays if that fails.
            log.warning("language miss on %s: %r", model, answer["display"][:80])
            attempts[-1]["language_miss"] = True
            continue
        break

    fell_back = kept is None
    if kept:
        answer, ctx = kept
    else:
        # Show the data with no speech: components from whatever the best attempt looked up (an attempt
        # that errored out has an empty context, so take the one with the most results).
        ctx = max(contexts, key=lambda c: len(c.results))
        auto, seen = [], set()
        for rid, r in ctx.results.items():
            if not _default_component(r["tool"]):
                continue
            if r["tool"] == "find_payer" and r["data"].get("single_match"):
                continue  # a settled search: the customer's own card says it
            key = (r["tool"], json.dumps({k: v for k, v in r["data"].items() if not k.startswith("_")},
                                         sort_keys=True, default=str))
            if key not in seen:  # the same lookup twice shows once
                seen.add(key)
                auto.append({"type": _default_component(r["tool"]), "result_id": rid})
        found, sorry = FALLBACK_TEXT.get(q_lang, FALLBACK_TEXT["en"])
        answer = {"display": found if auto else sorry, "speech": "",
                  "lang": "en-IN" if q_lang == "en" or q_lang not in FALLBACK_TEXT else f"{q_lang}-IN",
                  "components": auto, "follow_ups": []}
    if not fell_back and q_lang == "en" and answer["speech"] and reply_language(answer["speech"]) == "hi":
        if hinglish_words(answer["display"]) < 2:
            # English display, Hinglish speech: speak the (already guarded) English display instead.
            answer["speech"], answer["lang"] = answer["display"], "en-IN"
        else:  # both models answered in Hinglish: at least speak it with the Hindi voice and say so
            answer["lang"] = "hi-IN"
    allowed = grounded_numbers(visible_sources(ctx) + allowed_extra)
    follow_ups = [f for f in answer.get("follow_ups", [])[:2] if not ungrounded(f, allowed)]
    components = build_components(answer.get("components", []), ctx)
    if not components and not ctx.pending:
        # Nothing usable attached (none, or a pairing the server drops). A stated answer gets the default view
        # of its latest lookup; a search that didn't settle on one customer ("which Ram?") gets the choices.
        data = [(rid, r) for rid, r in ctx.results.items() if r["tool"] != "find_payer" and _default_component(r["tool"])]
        if data and not fell_back and answer_numbers(answer["display"]):
            rid, r = data[-1]
            components = build_components([{"type": _default_component(r["tool"]), "result_id": rid}], ctx)
        else:
            for rid, r in ctx.results.items():
                if r["tool"] == "find_payer" and not r["data"]["single_match"] and r["data"]["matches"]:
                    components += build_components([{"type": "chips", "result_id": rid}], ctx)
                    break
    pending = ctx.pending[-1] if ctx.pending else None
    if pending and not any(c["type"] == "confirm_action" for c in components):
        components.append({"type": "confirm_action", "data": pending})

    latency = round((time.monotonic() - started) * 1000)
    payload = {
        "display": answer["display"],
        "speech": answer["speech"] or None,
        "lang": answer.get("lang", "en-IN"),
        "components": components,
        "follow_ups": follow_ups,
        "pending_action": pending,
        "model": attempts[-1]["model"] if attempts else None,
        "fallback": fell_back,
    }
    row = await fetch_one(
        conn,
        "insert into agent_queries (merchant_id, transcript, lang, tool_calls, answer, model, latency_ms, cost_paise) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (merchant_id, question, payload["lang"],
         Jsonb({"calls": ctx.calls, "all_calls": [c.calls for c in contexts], "attempts": attempts}),
         Jsonb({k: v for k, v in payload.items() if k != "components"}), payload["model"], latency,
         sum(a.get("cost_paise", 0) for a in attempts)),
    )
    payload["query_id"] = str(row["id"])
    payload["latency_ms"] = latency
    return payload


def _default_component(tool: str) -> str | None:
    return {"get_payer_summary": "payer_card", "list_transactions": "table", "list_dues": "table",
            "get_sales_summary": "line_chart", "find_payer": "chips"}.get(tool) or (
        "confirm_action" if tool in WRITE_TOOLS else None)



async def warm_up() -> None:
    """Compile the structured-output schema and prime the prompt cache once at startup
    (a cold first question otherwise pays a one-time schema compile of tens of seconds)."""
    s = get_settings()
    if not s.anthropic_api_key or not s.agent_warmup:
        return
    for model in (s.agent_model, s.agent_fallback_model):
        try:
            await client().messages.create(
                model=model, max_tokens=300,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=_cached_tools(), tool_choice={"type": "none"},
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
                messages=[{"role": "user", "content": "Warm-up check: reply with display 'ok', empty speech, no components."}],
            )
            log.info("agent warm-up done for %s", model)
        except anthropic.APIError as e:
            log.warning("agent warm-up failed for %s: %s", model, e)
