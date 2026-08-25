"""Tests for Haiku extraction, its validation layer, and its fallbacks.

Bedrock is stubbed throughout - these tests assert our handling of what the
model returns, not the model's accuracy. The fixture tests double as a record of
what each provider's mail looks like.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from extract import (
    HEAD_CHARS,
    HIGH,
    LOW,
    SOURCE_BEDROCK,
    SOURCE_FALLBACK,
    TAIL_CHARS,
    Extraction,
    billing_month,
    extract,
    prepare_body,
)
from validate import amount_appears, largest_amount

FIXTURES = Path(__file__).parent / "fixtures"
MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


class StubBedrock:
    """Returns a canned tool-use response, or raises."""

    def __init__(self, tool_input=None, *, raises: Exception | None = None,
                 prose: bool = False):
        self.tool_input = tool_input or {}
        self.raises = raises
        self.prose = prose
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        if self.prose:
            return {
                "output": {
                    "message": {"content": [{"text": "I can't help with that."}]}
                }
            }
        return {
            "output": {
                "message": {
                    "content": [
                        {"toolUse": {"name": "record_bill", "input": self.tool_input}}
                    ]
                }
            }
        }


class ThrottlingException(Exception):
    pass


# --- happy path ---------------------------------------------------------------


def test_clean_extraction():
    client = StubBedrock(
        {
            "amount": "142.53",
            "due_date": "2026-09-08",
            "classification": "bill_due",
            "confidence": HIGH,
        }
    )
    result = extract(fixture("pge"), model_id=MODEL, client=client)

    assert result.amount == Decimal("142.53")
    assert result.due_date == date(2026, 9, 8)
    assert result.classification == "bill_due"
    assert result.confidence == HIGH
    assert result.source == SOURCE_BEDROCK
    assert result.is_billable


def test_output_is_schema_forced():
    """toolChoice pins the model to the tool, so we never parse free-form JSON."""
    client = StubBedrock({"amount": "1.00", "classification": "bill_due",
                          "confidence": HIGH, "due_date": None})
    extract("Amount due $1.00", model_id=MODEL, client=client)

    call = client.calls[0]
    assert call["toolConfig"]["toolChoice"] == {"tool": {"name": "record_bill"}}
    assert call["inferenceConfig"]["temperature"] == 0
    assert call["modelId"] == MODEL
    schema = call["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert set(schema["required"]) == {
        "amount",
        "due_date",
        "classification",
        "confidence",
    }


def test_subject_and_sender_are_given_to_the_model():
    client = StubBedrock({"amount": "1.00", "classification": "bill_due",
                          "confidence": HIGH, "due_date": None})
    extract(
        "Amount due $1.00",
        model_id=MODEL,
        client=client,
        subject="Your bill is ready",
        sender="billpay.pge.com",
    )
    prompt = client.calls[0]["messages"][0]["content"][0]["text"]
    assert "From: billpay.pge.com" in prompt
    assert "Subject: Your bill is ready" in prompt


# --- the validation layer -----------------------------------------------------


def test_amount_present_in_body_keeps_high_confidence():
    client = StubBedrock(
        {"amount": "44.05", "due_date": "2026-09-11",
         "classification": "bill_due", "confidence": HIGH}
    )
    assert extract(fixture("socalgas"), model_id=MODEL, client=client).confidence == HIGH


def test_amount_absent_from_body_downgrades_confidence():
    """The hallucination guard: a number not in the email can't be trusted."""
    client = StubBedrock(
        {"amount": "999.99", "due_date": None,
         "classification": "bill_due", "confidence": HIGH}
    )
    result = extract(fixture("socalgas"), model_id=MODEL, client=client)

    assert result.amount == Decimal("999.99")  # kept, not discarded
    assert result.confidence == LOW
    assert "not found in the email text" in result.notes
    # Still billable - a flagged bill beats a silently dropped one.
    assert result.is_billable


def test_model_reported_low_confidence_is_honored():
    client = StubBedrock(
        {"amount": "44.05", "due_date": None,
         "classification": "bill_due", "confidence": LOW}
    )
    assert extract(fixture("socalgas"), model_id=MODEL, client=client).confidence == LOW


def test_comma_formatted_amount_still_validates():
    client = StubBedrock(
        {"amount": "1234.56", "due_date": None,
         "classification": "bill_due", "confidence": HIGH}
    )
    result = extract("Total due: $1,234.56", model_id=MODEL, client=client)
    assert result.amount == Decimal("1234.56")
    assert result.confidence == HIGH


# --- classifications ----------------------------------------------------------


@pytest.mark.parametrize(
    ("classification", "billable"),
    [
        ("bill_due", True),
        ("bill_upcoming", True),
        ("payment_confirmation", False),
        ("other", False),
    ],
)
def test_classification_drives_billability(classification, billable):
    client = StubBedrock(
        {"amount": "10.00", "due_date": None,
         "classification": classification, "confidence": HIGH}
    )
    result = extract("Amount $10.00", model_id=MODEL, client=client)
    assert result.classification == classification
    assert result.is_billable is billable


def test_unknown_classification_becomes_other():
    client = StubBedrock(
        {"amount": "10.00", "due_date": None,
         "classification": "invented_category", "confidence": HIGH}
    )
    result = extract("Amount $10.00", model_id=MODEL, client=client)
    assert result.classification == "other"
    assert not result.is_billable


def test_zero_and_negative_amounts_are_not_billable():
    for amount in ("0.00", "-5.00"):
        client = StubBedrock(
            {"amount": amount, "due_date": None,
             "classification": "bill_due", "confidence": HIGH}
        )
        result = extract("Amount due $0.00", model_id=MODEL, client=client)
        assert result.amount is None
        assert not result.is_billable


# --- malformed model output ----------------------------------------------------


@pytest.mark.parametrize("value", ["not-a-number", "", "$$$", None])
def test_unparseable_amount_becomes_none(value):
    client = StubBedrock(
        {"amount": value, "due_date": None,
         "classification": "bill_due", "confidence": HIGH}
    )
    result = extract("Amount due $10.00", model_id=MODEL, client=client)
    assert result.amount is None
    assert not result.is_billable


@pytest.mark.parametrize("value", ["September 8", "2026-13-45", "soon", ""])
def test_unparseable_due_date_becomes_none_without_losing_the_bill(value):
    client = StubBedrock(
        {"amount": "10.00", "due_date": value,
         "classification": "bill_due", "confidence": HIGH}
    )
    result = extract("Amount due $10.00", model_id=MODEL, client=client)
    assert result.due_date is None
    assert result.amount == Decimal("10.00")
    assert result.is_billable


def test_amount_with_currency_symbol_is_tolerated():
    client = StubBedrock(
        {"amount": "$1,234.56", "due_date": None,
         "classification": "bill_due", "confidence": HIGH}
    )
    result = extract("Total $1,234.56", model_id=MODEL, client=client)
    assert result.amount == Decimal("1234.56")


# --- Bedrock failures fall back rather than raising ---------------------------


def test_throttling_falls_back_to_regex_sweep():
    client = StubBedrock(raises=ThrottlingException("Rate exceeded"))
    result = extract(fixture("watersewer"), model_id=MODEL, client=client)

    assert result.source == SOURCE_FALLBACK
    assert result.confidence == LOW
    assert result.amount == Decimal("109.43")  # largest figure in that fixture
    assert "ThrottlingException" in result.notes
    assert result.is_billable


def test_network_error_falls_back():
    result = extract(
        fixture("socalgas"), model_id=MODEL, client=StubBedrock(raises=OSError("boom"))
    )
    assert result.source == SOURCE_FALLBACK
    assert result.confidence == LOW


def test_refusal_without_tool_call_falls_back():
    """Model answered in prose instead of calling the tool."""
    result = extract(fixture("socalgas"), model_id=MODEL, client=StubBedrock(prose=True))
    assert result.source == SOURCE_FALLBACK
    assert result.confidence == LOW


def test_fallback_on_body_with_no_amounts_is_not_billable():
    result = extract(
        "Thanks for going paperless.", model_id=MODEL,
        client=StubBedrock(raises=OSError("boom")),
    )
    assert result.amount is None
    assert result.classification == "other"
    assert not result.is_billable


def test_empty_body_short_circuits_without_calling_bedrock():
    client = StubBedrock({"amount": "1.00"})
    result = extract("   ", model_id=MODEL, client=client)
    assert client.calls == []
    assert result.notes == "empty email body"
    assert not result.is_billable


# --- body preparation ---------------------------------------------------------


def test_plaintext_passes_through_untouched():
    assert prepare_body("Amount due: $10.00") == "Amount due: $10.00"


def test_html_body_is_converted_and_amount_becomes_findable():
    prepared = prepare_body(fixture("html_only"))
    assert "<table>" not in prepared
    assert "trackOpen" not in prepared
    assert amount_appears(prepared, "67.20")


def test_long_body_is_trimmed_from_the_middle():
    body = "TOTAL $50.00" + ("x" * 40000) + "DUE Sep 12"
    prepared = prepare_body(body)

    assert len(prepared) < len(body)
    assert "[...trimmed...]" in prepared
    # Both ends survive: totals sit near the top, due dates sometimes in footers.
    assert "TOTAL $50.00" in prepared
    assert "DUE Sep 12" in prepared
    assert len(prepared) <= HEAD_CHARS + TAIL_CHARS + 32


def test_body_at_the_threshold_is_not_trimmed():
    body = "a" * (HEAD_CHARS + TAIL_CHARS)
    assert "[...trimmed...]" not in prepare_body(body)


def test_prepare_body_handles_none_and_empty():
    assert prepare_body(None) == ""
    assert prepare_body("") == ""


# --- billing month ------------------------------------------------------------


@pytest.mark.parametrize(
    ("received", "tz", "expected"),
    [
        (datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc), "America/Los_Angeles", "2026-08"),
        (datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc), "America/Los_Angeles", "2026-01"),
        (datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc), "UTC", "2026-12"),
    ],
)
def test_billing_month(received, tz, expected):
    assert billing_month(received, tz) == expected


def test_billing_month_uses_local_time_not_utc():
    """03:00 UTC on Sep 1 is still August in Los Angeles."""
    received = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    assert billing_month(received, "America/Los_Angeles") == "2026-08"
    assert billing_month(received, "UTC") == "2026-09"


def test_naive_datetime_is_treated_as_utc():
    assert billing_month(datetime(2026, 8, 18, 12, 0), "UTC") == "2026-08"


# --- fixture-driven provider coverage ----------------------------------------


@pytest.mark.parametrize(
    ("name", "amount", "due"),
    [
        ("pge", "142.53", "2026-09-08"),
        ("socalgas", "44.05", "2026-09-11"),
        ("watersewer", "109.43", "2026-09-02"),
        ("spectrum", "89.99", "2026-09-05"),
    ],
)
def test_each_provider_fixture_validates_its_amount(name, amount, due):
    """Given the right answer, our validation layer must accept it."""
    client = StubBedrock(
        {"amount": amount, "due_date": due,
         "classification": "bill_due", "confidence": HIGH}
    )
    result = extract(fixture(name), model_id=MODEL, client=client)
    assert result.amount == Decimal(amount)
    assert result.due_date == date.fromisoformat(due)
    assert result.confidence == HIGH, "amount should be found verbatim in the fixture"


def test_upcoming_notice_is_classified_as_upcoming_and_has_no_due_date():
    client = StubBedrock(
        {"amount": "142.53", "due_date": None,
         "classification": "bill_upcoming", "confidence": HIGH}
    )
    result = extract(fixture("upcoming_no_due_date"), model_id=MODEL, client=client)
    assert result.classification == "bill_upcoming"
    assert result.due_date is None
    assert result.is_billable


def test_largest_amount_heuristic_is_wrong_where_haiku_is_right():
    """Why the model earns its keep: the biggest figure is a stale balance."""
    body = fixture("tricky_largest_is_not_total")
    assert largest_amount(body) == Decimal("310.00")  # previous balance

    client = StubBedrock(
        {"amount": "85.50", "due_date": "2026-09-15",
         "classification": "bill_due", "confidence": HIGH}
    )
    result = extract(body, model_id=MODEL, client=client)
    assert result.amount == Decimal("85.50")
    assert result.confidence == HIGH


# --- dataclass ----------------------------------------------------------------


def test_extraction_is_immutable():
    result = Extraction(
        amount=Decimal("1"), due_date=None, classification="bill_due",
        confidence=HIGH, source=SOURCE_BEDROCK,
    )
    with pytest.raises(Exception):
        result.amount = Decimal("2")  # type: ignore[misc]
