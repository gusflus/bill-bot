"""Tests for weighted share allocation.

The invariant that matters: allocated amounts always sum to the bill total
exactly, for any combination of total and weights.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shares import (
    allocate,
    allocate_cents,
    format_money,
    from_cents,
    to_cents,
)


# --- the core invariant -------------------------------------------------------


@pytest.mark.parametrize(
    "total_cents",
    [0, 1, 2, 3, 7, 99, 100, 101, 10000, 10001, 14253, 999999],
)
@pytest.mark.parametrize(
    "weights",
    [
        [1],
        [1, 1],
        [1, 1, 1],
        [1, 1, 2],
        [1, 1, 1, 1, 1, 1],
        [2.5, 1, 1],
        [0.5, 0.25, 0.25],
        [3, 7],
        [1, 2, 3, 4, 5, 6],
    ],
)
def test_allocation_always_sums_to_total(total_cents, weights):
    parts = allocate_cents(total_cents, weights)
    assert sum(parts) == total_cents
    assert len(parts) == len(weights)
    assert all(p >= 0 for p in parts)


# --- specific splits ---------------------------------------------------------


def test_even_split_of_clean_total():
    assert allocate_cents(30000, [1, 1, 1]) == [10000, 10000, 10000]


def test_weighted_split_of_clean_total():
    # $100 across 1/1/2 -> $25 / $25 / $50
    assert allocate_cents(10000, [1, 1, 2]) == [2500, 2500, 5000]


def test_odd_cent_goes_to_largest_remainder():
    # $100.01 across 1/1/2: exact shares are 2500.25, 2500.25, 5000.5.
    # The .5 remainder is largest, so the stray cent lands there.
    assert allocate_cents(10001, [1, 1, 2]) == [2500, 2500, 5001]


def test_three_way_split_of_a_dollar():
    # 100 cents / 3 = 33.33 each; the leftover cent goes to the first by tie-break.
    assert allocate_cents(100, [1, 1, 1]) == [34, 33, 33]


def test_ties_break_by_config_order_not_arbitrarily():
    # All three remainders are identical (.3333), so the two spare cents must go
    # to the first two entries, deterministically.
    assert allocate_cents(101, [1, 1, 1]) == [34, 34, 33]


def test_single_receiver_gets_everything():
    assert allocate_cents(14253, [1]) == [14253]
    assert allocate_cents(14253, [7.5]) == [14253]


def test_zero_total_allocates_nothing():
    assert allocate_cents(0, [1, 1, 2]) == [0, 0, 0]


def test_fractional_weights_are_exact():
    # 0.1 is not representable in binary floating point; going through Fraction
    # keeps the remainders honest.
    parts = allocate_cents(1000, [0.1, 0.2])
    assert sum(parts) == 1000
    assert parts == [333, 667]


def test_six_equal_roommates_on_a_realistic_bill():
    # The old flat-split case: $142.53 six ways.
    parts = allocate_cents(14253, [1] * 6)
    assert sum(parts) == 14253
    assert sorted(parts) == [2375, 2375, 2375, 2376, 2376, 2376]


# --- dollars interface -------------------------------------------------------


def test_allocate_returns_dollar_decimals():
    parts = allocate("100.01", [1, 1, 2])
    assert parts == [Decimal("25.00"), Decimal("25.00"), Decimal("50.01")]
    assert sum(parts) == Decimal("100.01")


def test_allocate_accepts_decimal_and_int():
    assert sum(allocate(Decimal("59.99"), [1, 1])) == Decimal("59.99")
    assert sum(allocate(60, [1, 1, 1])) == Decimal("60.00")


def test_allocated_decimals_always_have_two_places():
    for part in allocate("33.33", [1, 1, 1]):
        assert part.as_tuple().exponent == -2


# --- input validation --------------------------------------------------------


def test_empty_weights_rejected():
    with pytest.raises(ValueError, match="cannot be empty"):
        allocate_cents(100, [])


@pytest.mark.parametrize("weights", [[0], [1, 0], [1, -1], [-2, 3]])
def test_non_positive_weight_rejected(weights):
    with pytest.raises(ValueError, match="every weight must be > 0"):
        allocate_cents(100, weights)


def test_negative_total_rejected():
    with pytest.raises(ValueError, match="cannot be negative"):
        allocate_cents(-1, [1])


def test_sub_cent_precision_rejected():
    """Better to fail loudly than silently round someone's share."""
    with pytest.raises(ValueError, match="sub-cent precision"):
        to_cents("10.005")


# --- money conversion and formatting -----------------------------------------


@pytest.mark.parametrize(
    ("dollars", "cents"),
    [("0", 0), ("0.01", 1), ("1", 100), ("142.53", 14253), ("1234.56", 123456)],
)
def test_cent_roundtrip(dollars, cents):
    assert to_cents(dollars) == cents
    assert from_cents(cents) == Decimal(dollars).quantize(Decimal("0.01"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0"), "$0.00"),
        (Decimal("23.76"), "$23.76"),
        (Decimal("1234.5"), "$1,234.50"),
        (Decimal("1234567.89"), "$1,234,567.89"),
        (42, "$42.00"),
    ],
)
def test_format_money(value, expected):
    assert format_money(value) == expected
