"""Tester M1 (re-test): app/money.py. "All money is stored as integer paise" (ARCHITECTURE, Data model);
format_inr documents Indian digit grouping and handles a sign.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.money import format_inr, paise_to_amount_str, paise_to_rupees, rupees_to_paise


@pytest.mark.parametrize("paise,expected", [
    (0, "₹0"),
    (5, "₹0.05"),
    (99, "₹0.99"),
    (100_00, "₹100"),
    (8_500_00, "₹8,500"),
    (11_700_00, "₹11,700"),
    (12_100_00, "₹12,100"),
    (1_23_456_50, "₹1,23,456.50"),
    (12_34_56_789_00, "₹12,34,56,789"),
])
def test_format_inr_non_negative(paise, expected):
    assert format_inr(paise) == expected


@pytest.mark.parametrize("paise,expected", [
    (-8_500_00, "-₹8,500"),
    (-1_234_50, "-₹1,234.50"),
    (-50, "-₹0.50"),
    (-12_34_567_25, "-₹12,34,567.25"),
])
def test_format_inr_negative_amounts(paise, expected):
    """format_inr has explicit sign handling, so negative amounts (e.g. a drop vs last week) must format."""
    assert format_inr(paise) == expected


@pytest.mark.parametrize("value,paise", [
    ("8500", 8_500_00), ("8500.00", 8_500_00), (100, 100_00), ("0.05", 5), ("12.345", 12_35),
    (Decimal("1.005"), 1_01), (0.1 + 0.2, 30), ("0", 0),
])
def test_rupees_to_paise_is_exact_integer(value, paise):
    out = rupees_to_paise(value)
    assert out == paise and type(out) is int


def test_paise_round_trips_and_paytm_amount_string():
    for p in (0, 1, 99, 100, 8_500_00, 1_23_456_50):
        assert rupees_to_paise(paise_to_rupees(p)) == p
    assert paise_to_amount_str(8_500_00) == "8500.00"
    assert paise_to_amount_str(5) == "0.05"
    assert paise_to_amount_str(3_200_00) == "3200.00"
