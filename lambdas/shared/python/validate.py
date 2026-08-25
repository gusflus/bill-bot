"""Checking an extracted amount against the raw email text.

Haiku does the extracting. This module's only job is to confirm the number it
returned actually appears in the email, which catches the failure mode that
matters: a model reconstructing or hallucinating a total rather than reading
one. Cheap, deterministic, and independent of the model.

A miss is *not* proof of error - some senders render the amount as an image, or
split it across HTML so the plaintext never contains it. So a miss downgrades
confidence rather than rejecting the bill, because a false rejection means a
silently missed bill, which is worse than a flagged notification.

Pure functions, no dependencies.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Matches 1234.56, 1,234.56, $1,234.56, $ 1234.56. Requires exactly two decimal
# places, which is what keeps it from matching dates, phone numbers, or the
# account numbers that litter these emails.
_AMOUNT_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})")


def find_amounts(text: str) -> list[Decimal]:
    """Every currency-shaped number in ``text``, in order of appearance.

    Duplicates are kept - a total repeated three times in a bill is a signal,
    not noise.

    >>> find_amounts("Total $1,234.56 due, min payment 25.00")
    [Decimal('1234.56'), Decimal('25.00')]
    """
    amounts = []
    for raw in _AMOUNT_RE.findall(text or ""):
        try:
            amounts.append(Decimal(raw.replace(",", "")))
        except InvalidOperation:  # pragma: no cover - regex shouldn't allow this
            continue
    return amounts


def amount_appears(text: str, amount: Decimal | int | str) -> bool:
    """Whether ``amount`` appears in ``text`` as a currency-shaped number.

    Compared numerically, so '$1,234.56' in the email matches Decimal('1234.56')
    regardless of comma or dollar-sign formatting.

    >>> amount_appears("Amount due: $1,234.56", "1234.56")
    True
    >>> amount_appears("Amount due: $1,234.56", "99.99")
    False
    """
    try:
        target = Decimal(str(amount))
    except InvalidOperation:
        return False
    return any(found == target for found in find_amounts(text))


def largest_amount(text: str) -> Decimal | None:
    """The biggest currency-shaped number in ``text``, or None if there are none.

    Used only as a last-resort fallback when Bedrock is unreachable: for most
    utility bills the total is the largest figure in the email. Good enough to
    avoid dropping a bill entirely, not good enough to trust unflagged.
    """
    amounts = find_amounts(text)
    return max(amounts) if amounts else None
