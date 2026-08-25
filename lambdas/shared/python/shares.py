"""Splitting a bill into per-roommate amounts that sum to the total exactly.

Shares are relative weights, normalized against their own sum: all 1 means an
even split, a 2 pays double a 1. They never have to add up to any particular
number, which is why weights beat percentages - percentages can be configured
not to sum to 100, weights can't be wrong.

Cents are allocated by the largest-remainder method, so the per-person amounts
always sum to the bill total to the penny. The old Apps Script version just
rounded each share independently and the README conceded the total "may be off
by a few cents" - tolerable for a flat six-way split, visibly wrong once the
shares are unequal.

Pure functions, no dependencies. All arithmetic is exact: integer cents and
Fraction weights, never floats.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Sequence


def to_cents(amount: Decimal | int | str) -> int:
    """Dollars to whole cents. Rejects sub-cent precision rather than rounding."""
    value = Decimal(str(amount))
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"amount {amount!r} has sub-cent precision")
    return int(cents)


def from_cents(cents: int) -> Decimal:
    """Whole cents back to a 2-decimal-place Decimal."""
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def allocate_cents(total_cents: int, weights: Sequence[float]) -> list[int]:
    """Split ``total_cents`` across ``weights``, summing to it exactly.

    Each recipient gets the floor of their exact share, then the leftover
    pennies go to the largest fractional remainders. Ties break by position, so
    the result is deterministic for a given config ordering rather than
    depending on dict iteration or sort stability.

    >>> allocate_cents(10001, [1, 1, 2])
    [2500, 2500, 5001]
    >>> sum(allocate_cents(10001, [1, 1, 1]))
    10001
    """
    if not weights:
        raise ValueError("weights cannot be empty")
    if any(w <= 0 for w in weights):
        raise ValueError(f"every weight must be > 0, got {list(weights)}")
    if total_cents < 0:
        raise ValueError(f"total_cents cannot be negative, got {total_cents}")

    # Fraction keeps this exact; float weights like 2.5 or 0.1 would otherwise
    # produce remainders that depend on binary rounding.
    fractions = [Fraction(str(w)) for w in weights]
    weight_sum = sum(fractions)

    exact = [Fraction(total_cents) * f / weight_sum for f in fractions]
    floors = [int(e) for e in exact]  # Fraction -> int truncates toward zero
    leftover = total_cents - sum(floors)

    # Largest remainder first; earlier config entries win ties.
    order = sorted(
        range(len(weights)),
        key=lambda i: (-(exact[i] - floors[i]), i),
    )
    for i in order[:leftover]:
        floors[i] += 1

    return floors


def allocate(total: Decimal | int | str, weights: Sequence[float]) -> list[Decimal]:
    """``allocate_cents`` in dollars. Returns 2-decimal-place Decimals."""
    return [from_cents(c) for c in allocate_cents(to_cents(total), weights)]


def format_money(amount: Decimal | int, *, symbol: str = "$") -> str:
    """1234.5 -> '$1,234.50'. Thousands separated, always two decimals."""
    return f"{symbol}{Decimal(str(amount)):,.2f}"
