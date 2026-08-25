"""Tests for the ingest Lambda: auth, routing, and the status-code contract.

The status code is a contract with Apps Script (2xx label Processed, 4xx label
Error, 5xx leave for retry), so the code returned for each situation is asserted
deliberately rather than incidentally.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from runtime_config import RuntimeConfig
from store import bill_pk, get_bill, load_ledger
from tests.conftest import load_handler
from tests.test_store import seed_config

ingest = load_handler("ingest")

SECRET = "test-secret-value"

MESSAGES = {
    "bill": (
        "{biller} bill received: {total} total.\n"
        "Your share, {label}: {amount} - due {due_date}\n"
        "Pay: {venmo_link}{zelle_line}"
    ),
    "paid_ack": "Thanks {label}! {paid_count} of {receiver_count} paid.",
    "status": "{biller} {month}: {paid_count} of {receiver_count} paid.",
    "reminder": "Reminder: {amount} for {biller}. {venmo_link}",
    "payment_alert": "{label} paid {amount}. {paid_count} of {receiver_count} in.",
    "venmo_note": "{biller} bill split",
}


def config(**overrides) -> RuntimeConfig:
    base = {
        "table_name": "bill-bot-test",
        "model_id": "test-model",
        "timezone": "America/Los_Angeles",
        "dry_run": True,
        "on_low_confidence": "send",
        "no_due_date_text": "date unknown",
        "record_ttl_days": 400,
        "venmo_username": "gus-handle",
        "zelle_contact": None,
        "origination_number_id": "",
        "payer_share": 0.0,
        "secret_arn": "arn:aws:secretsmanager:us-west-2:1:secret:x",
        "messages": MESSAGES,
    }
    return RuntimeConfig(**{**base, **overrides})


class StubExtract:
    """Replaces extract.extract with a fixed result."""

    def __init__(self, **fields):
        from extract import Extraction

        defaults = {
            "amount": Decimal("142.53"),
            "due_date": date(2026, 9, 8),
            "classification": "bill_due",
            "confidence": "high",
            "source": "bedrock",
            "notes": "",
        }
        self.result = Extraction(**{**defaults, **fields})
        self.calls = []

    def __call__(self, body, **kwargs):
        self.calls.append((body, kwargs))
        return self.result


def payload(**overrides) -> dict:
    base = {
        "messageId": "msg-1",
        "threadId": "thread-1",
        "from": "PG&E <no-reply@billpay.pge.com>",
        "subject": "Your bill is ready",
        "receivedAt": "2026-08-18T12:00:00Z",
        "bodyText": "Amount due $142.53 by September 08, 2026",
    }
    return {**base, **overrides}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setattr(ingest, "_shared_secret", lambda arn: SECRET)
    monkeypatch.setattr(ingest, "_secret_cache", SECRET)


def event(*, method="POST", path="/bills", body=None, secret=SECRET) -> dict:
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers["x-bill-bot-secret"] = secret
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
    }


def body_of(response) -> dict:
    return json.loads(response["body"])


# --- auth ---------------------------------------------------------------------


def test_missing_secret_is_401(state_table, monkeypatch):
    monkeypatch.setenv("SECRET_ARN", "arn:x")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    response = ingest.handler(event(secret=None), None)
    assert response["statusCode"] == 401


def test_wrong_secret_is_401(state_table, monkeypatch):
    monkeypatch.setenv("SECRET_ARN", "arn:x")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    response = ingest.handler(event(secret="wrong"), None)
    assert response["statusCode"] == 401


def test_correct_secret_is_accepted(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setenv("SECRET_ARN", "arn:x")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    response = ingest.handler(event(method="GET", path="/senders"), None)
    assert response["statusCode"] == 200


def test_authorized_uses_constant_time_comparison():
    """Guards against a refactor to ==, which would leak the secret by timing."""
    import inspect

    source = inspect.getsource(ingest._authorized)
    assert "compare_digest" in source


# --- GET /senders -------------------------------------------------------------


def test_get_senders_returns_the_configured_list(state_table):
    seed_config(state_table)
    response = ingest.handle_get_senders(state_table)
    assert response["statusCode"] == 200

    senders = body_of(response)["senders"]
    assert {s["fromAddress"] for s in senders} == {
        "billpay.pge.com",
        "socalgas.com",
    }
    # Apps Script reads fromAddress, so the key name is part of the contract.
    assert all({"id", "name", "fromAddress"} <= set(s) for s in senders)


# --- happy path ---------------------------------------------------------------


def test_new_bill_writes_records_and_reports_dry_run(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract())

    response = ingest.handle_post_bill(state_table, payload(), config())
    assert response["statusCode"] == 200

    result = body_of(response)
    assert result["action"] == "new"
    assert result["bill"] == "BILL#pg-e#2026-08"
    assert result["dryRun"] is True
    assert result["failed"] == []

    bill = get_bill(state_table, "BILL#pg-e#2026-08")
    assert bill["total"] == Decimal("142.53")
    assert bill["due_date"] == "2026-09-08"

    ledger = load_ledger(state_table, "BILL#pg-e#2026-08")
    assert len(ledger) == 3
    assert sum(r["amount_owed"] for r in ledger) == Decimal("142.53")
    assert all(r["paid"] is False for r in ledger)


def test_weighted_shares_reach_the_ledger(state_table, monkeypatch):
    """Alex has share 2 of 4 total, so owes half."""
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract(amount=Decimal("100.00")))

    ingest.handle_post_bill(state_table, payload(), config())
    ledger = {r["label"]: r["amount_owed"] for r in
              load_ledger(state_table, "BILL#pg-e#2026-08")}
    assert ledger == {
        "Gus": Decimal("25.00"),
        "Sam": Decimal("25.00"),
        "Alex": Decimal("50.00"),
    }


def test_billing_month_uses_the_configured_timezone(state_table, monkeypatch):
    """03:00 UTC Sep 1 is still August in Los Angeles."""
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract())

    response = ingest.handle_post_bill(
        state_table, payload(receivedAt="2026-09-01T03:00:00Z"), config()
    )
    assert body_of(response)["bill"] == "BILL#pg-e#2026-08"


def test_missing_received_at_falls_back_to_now(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract())
    response = ingest.handle_post_bill(
        state_table, payload(receivedAt=None), config()
    )
    assert response["statusCode"] == 200


# --- dedup through the handler ------------------------------------------------


def test_replayed_email_is_reported_as_a_replay(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract())

    first = ingest.handle_post_bill(state_table, payload(), config())
    second = ingest.handle_post_bill(state_table, payload(), config())

    assert body_of(first)["action"] == "new"
    assert body_of(second)["action"] == "replay"
    # Still a success: Apps Script should label it, not retry it.
    assert second["statusCode"] == 200


def test_upcoming_then_due_notifies_once(state_table, monkeypatch):
    seed_config(state_table)

    monkeypatch.setattr(
        ingest, "extract",
        StubExtract(classification="bill_upcoming", due_date=None),
    )
    first = ingest.handle_post_bill(state_table, payload(messageId="soon"), config())

    monkeypatch.setattr(ingest, "extract", StubExtract(classification="bill_due"))
    second = ingest.handle_post_bill(state_table, payload(messageId="due"), config())

    assert body_of(first)["action"] == "new"
    assert body_of(second)["action"] == "duplicate"
    assert second["statusCode"] == 200


def test_changed_amount_sends_a_correction(state_table, monkeypatch):
    seed_config(state_table)

    monkeypatch.setattr(ingest, "extract", StubExtract(amount=Decimal("140.00")))
    ingest.handle_post_bill(state_table, payload(messageId="soon"), config())

    monkeypatch.setattr(ingest, "extract", StubExtract(amount=Decimal("142.53")))
    response = ingest.handle_post_bill(
        state_table, payload(messageId="due"), config()
    )

    assert body_of(response)["action"] == "corrected"
    ledger = load_ledger(state_table, "BILL#pg-e#2026-08")
    assert sum(r["amount_owed"] for r in ledger) == Decimal("142.53")


# --- not-a-bill paths ---------------------------------------------------------


@pytest.mark.parametrize("classification", ["payment_confirmation", "other"])
def test_non_bill_email_is_ignored_with_200(state_table, monkeypatch, classification):
    """A final answer, not a failure - so Apps Script labels it and moves on."""
    seed_config(state_table)
    monkeypatch.setattr(
        ingest, "extract", StubExtract(classification=classification)
    )

    response = ingest.handle_post_bill(state_table, payload(), config())
    assert response["statusCode"] == 200
    assert body_of(response)["action"] == "ignored"
    assert get_bill(state_table, "BILL#pg-e#2026-08") is None


def test_unextractable_amount_is_ignored_with_200(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract(amount=None))
    response = ingest.handle_post_bill(state_table, payload(), config())
    assert response["statusCode"] == 200
    assert body_of(response)["action"] == "ignored"


# --- low confidence -----------------------------------------------------------


def test_low_confidence_sends_with_a_warning_by_default(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract(confidence="low"))

    response = ingest.handle_post_bill(state_table, payload(), config())
    assert body_of(response)["action"] == "new"
    assert body_of(response)["confidence"] == "low"


def test_low_confidence_holds_when_configured(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(
        ingest, "extract",
        StubExtract(confidence="low", notes="amount not found in the email text"),
    )

    response = ingest.handle_post_bill(
        state_table, payload(), config(on_low_confidence="hold")
    )
    result = body_of(response)
    assert response["statusCode"] == 200
    assert result["action"] == "held"

    # Recorded so you can look at it, but nobody was texted.
    bill = get_bill(state_table, "BILL#pg-e#2026-08")
    assert bill["status"] == "held"


def test_high_confidence_is_not_held(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract(confidence="high"))
    response = ingest.handle_post_bill(
        state_table, payload(), config(on_low_confidence="hold")
    )
    assert body_of(response)["action"] == "new"


# --- client errors (4xx: Apps Script labels Error) ----------------------------


def test_unknown_sender_is_400(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract())
    response = ingest.handle_post_bill(
        state_table, payload(**{"from": "spam <hi@example.com>"}), config()
    )
    assert response["statusCode"] == 400
    assert "no configured sender" in body_of(response)["error"]


def test_missing_message_id_is_400(state_table):
    seed_config(state_table)
    response = ingest.handle_post_bill(
        state_table, payload(messageId=None), config()
    )
    assert response["statusCode"] == 400


def test_invalid_json_body_is_400(state_table, monkeypatch):
    monkeypatch.setenv("SECRET_ARN", "arn:x")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    bad = event()
    bad["body"] = "{not json"
    response = ingest.handler(bad, None)
    assert response["statusCode"] == 400


def test_non_object_json_body_is_400(state_table, monkeypatch):
    monkeypatch.setenv("SECRET_ARN", "arn:x")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    response = ingest.handler(event(body=["a", "list"]), None)
    assert response["statusCode"] == 400


def test_oversized_body_is_413_before_any_work(state_table, monkeypatch):
    monkeypatch.setenv("SECRET_ARN", "arn:x")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    huge = event()
    huge["body"] = "x" * (ingest.MAX_BODY_BYTES + 1)
    response = ingest.handler(huge, None)
    assert response["statusCode"] == 413


def test_unsupported_method_is_405(state_table, monkeypatch):
    monkeypatch.setenv("SECRET_ARN", "arn:x")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    response = ingest.handler(event(method="DELETE", path="/bills"), None)
    assert response["statusCode"] == 405


# --- server errors (5xx: Apps Script retries) --------------------------------


def test_no_receivers_configured_is_500(state_table, monkeypatch):
    """Seeding must have failed - worth retrying rather than losing the bill."""
    seed_config(state_table, receivers=[])
    monkeypatch.setattr(ingest, "extract", StubExtract())
    response = ingest.handle_post_bill(state_table, payload(), config())
    assert response["statusCode"] == 500


def test_unexpected_exception_is_500_not_a_crash(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setenv("SECRET_ARN", "arn:x")
    monkeypatch.setenv("TABLE_NAME", "bill-bot-test")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))

    def boom(*args, **kwargs):
        raise RuntimeError("bedrock exploded")

    monkeypatch.setattr(ingest, "handle_post_bill", boom)
    response = ingest.handler(event(body=payload()), None)
    assert response["statusCode"] == 500
    assert body_of(response)["error"] == "internal error"


# --- the payer's share --------------------------------------------------------


def test_payer_share_reduces_what_receivers_owe(state_table, monkeypatch):
    """Payer share 1 alongside three receivers: everyone owes a quarter."""
    seed_config(state_table, receivers=[
        ("Sam", "+15551230002", 1),
        ("Alex", "+15551230003", 1),
        ("Jo", "+15551230004", 1),
    ])
    monkeypatch.setattr(ingest, "extract", StubExtract(amount=Decimal("100.00")))

    response = ingest.handle_post_bill(
        state_table, payload(), config(payer_share=1.0)
    )
    assert body_of(response)["payerShare"] == "25.00"

    ledger = {r["label"]: r["amount_owed"] for r in
              load_ledger(state_table, "BILL#pg-e#2026-08")}
    assert ledger == {
        "Sam": Decimal("25.00"),
        "Alex": Decimal("25.00"),
        "Jo": Decimal("25.00"),
    }


def test_payer_gets_no_ledger_row(state_table, monkeypatch):
    """Nothing to collect from the person who paid the utility."""
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract(amount=Decimal("100.00")))

    ingest.handle_post_bill(state_table, payload(), config(payer_share=1.0))
    rows = load_ledger(state_table, "BILL#pg-e#2026-08")
    assert len(rows) == 3  # the three seeded receivers, not four
    assert "PAY#+15551230000" not in {r["SK"] for r in rows}


def test_ledger_plus_payer_share_reconciles_to_the_total(state_table, monkeypatch):
    """The ledger alone no longer sums to the total - the payer absorbs the rest."""
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract(amount=Decimal("100.01")))

    response = ingest.handle_post_bill(
        state_table, payload(), config(payer_share=1.0)
    )
    payer_amount = Decimal(body_of(response)["payerShare"])
    ledger_sum = sum(
        r["amount_owed"] for r in load_ledger(state_table, "BILL#pg-e#2026-08")
    )
    assert ledger_sum + payer_amount == Decimal("100.01")


def test_payer_amount_is_recorded_on_the_bill(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract(amount=Decimal("100.00")))

    ingest.handle_post_bill(state_table, payload(), config(payer_share=1.0))
    bill = get_bill(state_table, "BILL#pg-e#2026-08")
    assert bill["payer_amount"] == Decimal("20.00")  # 1 of 1+1+1+2 weights


def test_payer_share_zero_gives_receivers_the_whole_bill(state_table, monkeypatch):
    seed_config(state_table)
    monkeypatch.setattr(ingest, "extract", StubExtract(amount=Decimal("100.00")))

    response = ingest.handle_post_bill(
        state_table, payload(), config(payer_share=0.0)
    )
    assert body_of(response)["payerShare"] == "0.00"
    ledger_sum = sum(
        r["amount_owed"] for r in load_ledger(state_table, "BILL#pg-e#2026-08")
    )
    assert ledger_sum == Decimal("100.00")


def test_payer_never_receives_a_bill_text(state_table):
    seed_config(state_table)
    from store import load_receivers

    messages = ingest.build_messages(
        config=config(),
        sender_name="PG&E",
        total=Decimal("100.00"),
        amounts={r.phone: Decimal("25.00") for r in load_receivers(state_table)},
        receivers=load_receivers(state_table),
        month="2026-08",
        due_date=None,
    )
    assert "+15551230000" not in messages


def test_split_bill_is_exact_across_many_totals(state_table):
    seed_config(state_table)
    from store import load_receivers

    receivers = load_receivers(state_table)
    for cents in range(1, 400):
        total = Decimal(cents) / 100
        amounts, payer = ingest.split_bill(total, receivers, 1.5)
        assert sum(amounts.values()) + payer == total


# --- message construction -----------------------------------------------------


def test_messages_are_personalized_per_receiver(state_table):
    seed_config(state_table)
    from store import load_receivers

    receivers = load_receivers(state_table)
    messages = ingest.build_messages(
        config=config(zelle_contact="pay@example.com"),
        sender_name="SoCalGas",
        total=Decimal("100.00"),
        amounts={
            "+15551230001": Decimal("25.00"),
            "+15551230002": Decimal("25.00"),
            "+15551230003": Decimal("50.00"),
        },
        receivers=receivers,
        month="2026-08",
        due_date=date(2026, 9, 11),
    )

    assert set(messages) == {"+15551230001", "+15551230002", "+15551230003"}
    assert "Your share, Gus: $25.00" in messages["+15551230001"]
    assert "Your share, Alex: $50.00" in messages["+15551230003"]
    # Each link carries that person's own amount.
    assert "amount=25.00" in messages["+15551230001"]
    assert "amount=50.00" in messages["+15551230003"]
    assert "Or Zelle $50.00 to pay@example.com" in messages["+15551230003"]
    assert "due Sep 11" in messages["+15551230001"]



def test_missing_due_date_uses_the_configured_text(state_table):
    seed_config(state_table)
    from store import load_receivers

    messages = ingest.build_messages(
        config=config(),
        sender_name="PG&E",
        total=Decimal("100.00"),
        amounts={r.phone: Decimal("25.00") for r in load_receivers(state_table)},
        receivers=load_receivers(state_table),
        month="2026-08",
        due_date=None,
    )
    assert "due date unknown" in next(iter(messages.values()))


def test_correction_says_what_the_old_amount_was(state_table):
    """A second text with a different number is confusing without this."""
    seed_config(state_table)
    from store import load_receivers

    messages = ingest.build_messages(
        config=config(),
        sender_name="PG&E",
        total=Decimal("142.53"),
        amounts={r.phone: Decimal("35.63") for r in load_receivers(state_table)},
        receivers=load_receivers(state_table),
        month="2026-08",
        due_date=None,
        corrected_from=Decimal("140.00"),
    )
    assert next(iter(messages.values())).startswith("Correction (was $140.00).")
