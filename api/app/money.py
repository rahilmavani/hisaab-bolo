"""Money is integer paise everywhere; times are shown in IST."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def rupees_to_paise(value: str | float | int | Decimal) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def paise_to_rupees(paise: int) -> Decimal:
    return (Decimal(paise) / 100).quantize(Decimal("0.01"))


def paise_to_amount_str(paise: int) -> str:
    """'8500.00' — the 2-decimal string Paytm expects."""
    return f"{paise_to_rupees(paise):.2f}"


def format_inr(paise: int) -> str:
    """₹12,100 or ₹1,23,456.50 (Indian digit grouping); negatives as -₹1,234.50."""
    sign = "-" if paise < 0 else ""
    rupees = paise_to_rupees(abs(paise))
    whole = int(rupees)
    frac = rupees - whole
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    out = f"{sign}₹{s}"
    if frac:
        out += f"{frac:.2f}"[1:]
    return out


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)
