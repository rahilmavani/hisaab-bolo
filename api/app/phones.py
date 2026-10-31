"""Indian phone numbers: one canonical form so search and identity agree."""

import re

_PREFIX = re.compile(r"^\s*(?:\+\s*91|00\s*91)[\s-]*")


def india_digits(text: str) -> str:
    """Digits without the country code or trunk zero: '+91 98765-43210', '0091 98765 43210',
    '919876543210' and '09876543210' all become '9876543210'. Partial input works too
    ('+91 98765' → '98765')."""
    stripped = _PREFIX.sub("", text)
    digits = re.sub(r"\D", "", stripped)
    if stripped == text.lstrip() or stripped == text:  # no explicit +91 / 0091 prefix
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
    return digits


def looks_like_phone(text: str) -> bool:
    return bool(re.fullmatch(r"[+\d\s().-]{3,24}", text.strip())) and sum(c.isdigit() for c in text) >= 3
