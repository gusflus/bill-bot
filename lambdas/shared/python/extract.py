"""Reading a bill email with Bedrock Claude Haiku.

Haiku is the primary extractor: it reads the email and returns the amount, due
date, and what kind of email it is. A generic dollar-amount sweep then checks
that the number it returned actually appears in the text (see ``validate.py``).

That ordering is the opposite of what you might expect, and it's deliberate.
There is no per-sender regex to extract with - the setup wizard that used to
pick one is gone, because Haiku makes it unnecessary. So the regex became the
*validator*, guarding against a model reconstructing a total instead of reading
one. Two independent checks either way, and this way round the due date comes
free, which regex never handled well across formats.

A validation miss downgrades confidence rather than rejecting the bill. Some
senders render the total as an image, or split it across HTML so the plaintext
never contains it - a false rejection would silently lose a real bill, which is
worse than a flagged notification.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from html_to_text import html_to_text
from validate import amount_appears, largest_amount

logger = logging.getLogger(__name__)

# Bill emails are mostly boilerplate. The total is usually near the top and the
# due date sometimes sits in a footer, so keep both ends and drop the middle.
HEAD_CHARS = 8000
TAIL_CHARS = 2000

CLASSIFICATIONS = ("bill_due", "bill_upcoming", "payment_confirmation", "other")

HIGH = "high"
LOW = "low"

SOURCE_BEDROCK = "bedrock"
SOURCE_FALLBACK = "fallback"

_TOOL_NAME = "record_bill"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "amount": {
            "type": ["string", "null"],
            "description": (
                "The total amount the customer owes, as digits only with a "
                "decimal point and no currency symbol or thousands separators, "
                "e.g. '1234.56'. Copy it exactly as printed in the email - do "
                "not compute, round, or infer it. Null if the email states no "
                "amount owed."
            ),
        },
        "due_date": {
            "type": ["string", "null"],
            "description": (
                "The payment due date as YYYY-MM-DD. Null if the email does "
                "not state one."
            ),
        },
        "classification": {
            "type": "string",
            "enum": list(CLASSIFICATIONS),
            "description": (
                "bill_due: a bill is payable now. "
                "bill_upcoming: a bill is coming but not yet payable "
                "('your bill will be ready', 'autopay scheduled'). "
                "payment_confirmation: a receipt for a payment already made. "
                "other: anything else, including marketing and service notices."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": [HIGH, LOW],
            "description": (
                "high if the amount is stated plainly and unambiguously; low if "
                "you had to choose between competing figures or infer anything."
            ),
        },
    },
    "required": ["amount", "due_date", "classification", "confidence"],
}

_SYSTEM_PROMPT = (
    "You read utility bill emails and report what is owed. Extract only what "
    "the email literally states. Never calculate, estimate, or guess an "
    "amount. If several figures appear, choose the total the customer must pay "
    "now - not the previous balance, not a minimum payment, not usage charges, "
    "not a late fee. Always call the "
    f"{_TOOL_NAME} tool."
)


@dataclass(frozen=True)
class Extraction:
    """What we learned about one bill email."""

    amount: Decimal | None
    due_date: date | None
    classification: str
    confidence: str
    source: str
    notes: str = ""

    @property
    def is_billable(self) -> bool:
        """Whether this email should produce a notification at all."""
        return (
            self.amount is not None
            and self.amount > 0
            and self.classification in ("bill_due", "bill_upcoming")
        )


def prepare_body(raw: str) -> str:
    """Normalize an email body to plaintext and trim it for the prompt.

    Apps Script sends ``getPlainBody()``, which is already plaintext for almost
    every sender. The HTML path is for the ones whose plaintext part is empty or
    is just "view this in your browser".
    """
    text = raw or ""
    if "<" in text and (">" in text) and _looks_like_html(text):
        text = html_to_text(text)

    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text.strip()
    return (
        text[:HEAD_CHARS].strip()
        + "\n[...trimmed...]\n"
        + text[-TAIL_CHARS:].strip()
    )


def _looks_like_html(text: str) -> bool:
    lowered = text[:4000].lower()
    return any(tag in lowered for tag in ("<html", "<body", "<table", "<div", "<td"))


def billing_month(received_at: datetime, timezone: str) -> str:
    """The 'YYYY-MM' bucket a bill belongs to, in the household's timezone.

    Deliberately based on when the email arrived rather than the due date: a
    "bill coming soon" email often states no due date at all, so the received
    date is the only value both emails for one bill reliably share.

    Month boundaries are the known weak spot - a "coming soon" on Aug 31 and a
    "due" on Sep 1 land in different buckets. The ingest handler closes that by
    also checking the previous month for a matching amount before creating a
    new bill.
    """
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=ZoneInfo("UTC"))
    local = received_at.astimezone(ZoneInfo(timezone))
    return f"{local.year:04d}-{local.month:02d}"


def _parse_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        logger.warning("could not parse amount %r from model output", value)
        return None
    return amount if amount > 0 else None


def _parse_due_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        logger.warning("could not parse due_date %r from model output", value)
        return None


def _tool_input(response: dict[str, Any]) -> dict[str, Any]:
    """Pull the tool-use payload out of a Converse response."""
    for block in response.get("output", {}).get("message", {}).get("content", []):
        if "toolUse" in block:
            return block["toolUse"].get("input", {})
    # The model answered in prose instead of calling the tool - a refusal, or a
    # body so mangled it had nothing to report.
    raise ValueError("model did not call the tool")


def _fallback(body: str, reason: str) -> Extraction:
    """Best effort when Bedrock is unusable: take the largest figure, flag it.

    For most utility bills the total is the largest number in the email. Good
    enough not to drop the bill, nowhere near good enough to send unflagged.
    """
    amount = largest_amount(body)
    logger.warning("falling back to regex sweep (%s); amount=%s", reason, amount)
    return Extraction(
        amount=amount,
        due_date=None,
        classification="bill_due" if amount else "other",
        confidence=LOW,
        source=SOURCE_FALLBACK,
        notes=f"bedrock unavailable ({reason}); used largest amount in email",
    )


def extract(
    body: str,
    *,
    model_id: str,
    client: Any | None = None,
    subject: str = "",
    sender: str = "",
) -> Extraction:
    """Extract amount, due date, and classification from one bill email."""
    prepared = prepare_body(body)
    if not prepared:
        return Extraction(
            amount=None,
            due_date=None,
            classification="other",
            confidence=LOW,
            source=SOURCE_FALLBACK,
            notes="empty email body",
        )

    if client is None:
        import boto3

        client = boto3.client("bedrock-runtime")

    context = "\n".join(
        part for part in (f"From: {sender}" if sender else "",
                          f"Subject: {subject}" if subject else "") if part
    )
    prompt = f"{context}\n\n{prepared}" if context else prepared

    try:
        response = client.converse(
            modelId=model_id,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            toolConfig={
                "tools": [
                    {
                        "toolSpec": {
                            "name": _TOOL_NAME,
                            "description": "Record what this bill email says is owed.",
                            "inputSchema": {"json": _TOOL_SCHEMA},
                        }
                    }
                ],
                # Force structured output rather than hoping for well-formed JSON.
                "toolChoice": {"tool": {"name": _TOOL_NAME}},
            },
            inferenceConfig={"temperature": 0, "maxTokens": 512},
        )
        raw = _tool_input(response)
    except Exception as exc:  # noqa: BLE001 - throttling, refusal, malformed, network
        return _fallback(prepared, type(exc).__name__)

    classification = str(raw.get("classification", "other"))
    if classification not in CLASSIFICATIONS:
        logger.warning("unexpected classification %r, treating as other", classification)
        classification = "other"

    extraction = Extraction(
        amount=_parse_amount(raw.get("amount")),
        due_date=_parse_due_date(raw.get("due_date")),
        classification=classification,
        confidence=HIGH if raw.get("confidence") == HIGH else LOW,
        source=SOURCE_BEDROCK,
    )
    return _apply_validation(extraction, prepared)


def _apply_validation(extraction: Extraction, body: str) -> Extraction:
    """Downgrade confidence if the amount isn't in the email text."""
    if extraction.amount is None:
        return extraction
    if amount_appears(body, extraction.amount):
        return extraction
    return replace(
        extraction,
        confidence=LOW,
        notes=(
            f"amount {extraction.amount} was not found in the email text; "
            "it may have been misread or reconstructed"
        ),
    )


def _main() -> None:
    """`python -m extract <file>` - read a saved email body and report."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="file containing an email body")
    parser.add_argument(
        "--model",
        default=os.environ.get(
            "BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip Bedrock and show only what the regex sweep finds",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with open(args.path) as f:
        body = f.read()

    if args.offline:
        result = _fallback(prepare_body(body), "offline mode")
    else:
        result = extract(body, model_id=args.model)

    print(json.dumps(
        {
            "amount": str(result.amount) if result.amount is not None else None,
            "due_date": result.due_date.isoformat() if result.due_date else None,
            "classification": result.classification,
            "confidence": result.confidence,
            "source": result.source,
            "notes": result.notes,
            "is_billable": result.is_billable,
        },
        indent=2,
    ))


if __name__ == "__main__":
    _main()
