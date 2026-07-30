"""Library of candidate patterns the wizard tries against a sample bill email.

Each "labeled" candidate looks for a dollar amount near a common bill label
(e.g. "Total Amount Due"). The "generic" candidates fall back to picking a
dollar amount out of the document by position/size when no label matches.

A dollar amount always looks like $1,234.56 or $12.34 - group(1) is the
numeric text without the leading '$'.
"""
import re

AMOUNT = r"\$?\s*([\d,]+\.\d{2})"

LABELED_CANDIDATES = [
    {"id": "total-amount-due", "label": "Total Amount Due", "pattern": rf"Total\s+Amount\s+Due[:\s]*{AMOUNT}"},
    {"id": "amount-due", "label": "Amount Due", "pattern": rf"Amount\s+Due[:\s]*{AMOUNT}"},
    {"id": "total-balance", "label": "Total Balance", "pattern": rf"Total\s+Balance[:\s]*{AMOUNT}"},
    {"id": "balance-due", "label": "Balance Due", "pattern": rf"Balance\s+Due[:\s]*{AMOUNT}"},
    {"id": "total-current-charges", "label": "Total Current Charges", "pattern": rf"Total\s+Current\s+Charges[:\s]*{AMOUNT}"},
    {"id": "total-due", "label": "Total Due", "pattern": rf"Total\s+Due[:\s]*{AMOUNT}"},
    {"id": "payment-amount", "label": "Payment Amount", "pattern": rf"Payment\s+Amount[:\s]*{AMOUNT}"},
    {"id": "current-balance", "label": "Current Balance", "pattern": rf"Current\s+Balance[:\s]*{AMOUNT}"},
    {"id": "new-charges", "label": "New Charges", "pattern": rf"New\s+Charges[:\s]*{AMOUNT}"},
]

# Generic fallbacks: no label lookup, just pick a $ amount out of every $ amount
# found in the document by position or size. Useful for senders like PG&E whose
# amount has no adjacent text label at all (it's an image in the original HTML).
GENERIC_CANDIDATES = [
    {"id": "generic-first", "label": "First $ amount in the email"},
    {"id": "generic-last", "label": "Last $ amount in the email"},
    {"id": "generic-largest", "label": "Largest $ amount in the email"},
]

_ALL_AMOUNTS_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")


def _to_float(amount_text: str) -> float:
    return float(amount_text.replace(",", ""))


def find_all_dollar_amounts(text: str):
    """Returns [(amount_text, start_index), ...] for every $X.XX in text."""
    return [(m.group(1), m.start()) for m in _ALL_AMOUNTS_RE.finditer(text)]


def try_candidate(candidate: dict, text: str):
    """Runs one candidate against text. Returns {amount, context} or None."""
    amounts = find_all_dollar_amounts(text)

    if candidate["id"] == "generic-first":
        if not amounts:
            return None
        amount_text, idx = amounts[0]
    elif candidate["id"] == "generic-last":
        if not amounts:
            return None
        amount_text, idx = amounts[-1]
    elif candidate["id"] == "generic-largest":
        if not amounts:
            return None
        amount_text, idx = max(amounts, key=lambda pair: _to_float(pair[0]))
    else:
        match = re.search(candidate["pattern"], text, re.IGNORECASE)
        if not match:
            return None
        amount_text, idx = match.group(1), match.start()

    context_start = max(0, idx - 40)
    context_end = min(len(text), idx + 40)
    context = text[context_start:context_end].replace("\n", " ").strip()

    return {"amount": _to_float(amount_text), "amount_text": amount_text, "context": context}


def all_candidates():
    return LABELED_CANDIDATES + GENERIC_CANDIDATES
