"""Tests for SMS fan-out and dry-run behavior."""

from __future__ import annotations

import logging

import pytest

from sender import Delivery, send_all

NUMBER = "phone-abc123"
MESSAGES = {
    "+15551230001": "Gus owes $25.00",
    "+15551230002": "Sam owes $25.00",
    "+15551230003": "Alex owes $50.00",
}


class StubSms:
    def __init__(self, fail_for: set[str] | None = None):
        self.calls: list[dict] = []
        self.fail_for = fail_for or set()

    def send_text_message(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["DestinationPhoneNumber"] in self.fail_for:
            raise RuntimeError("carrier rejected")
        return {"MessageId": "mid-1"}


# --- dry run ------------------------------------------------------------------


def test_dry_run_sends_nothing(caplog):
    client = StubSms()
    with caplog.at_level(logging.INFO):
        delivery = send_all(
            MESSAGES, dry_run=True, origination_number_id=NUMBER, client=client
        )

    assert client.calls == []
    assert delivery.sent == []
    assert len(delivery.logged) == 3
    assert delivery.dry_run


def test_dry_run_logs_the_actual_message_text(caplog):
    with caplog.at_level(logging.INFO):
        send_all(MESSAGES, dry_run=True, origination_number_id=NUMBER)
    logged = caplog.text
    assert "Alex owes $50.00" in logged
    assert "+15551230003" in logged


def test_dry_run_needs_no_origination_number():
    """So the whole pipeline is testable before a number is provisioned."""
    delivery = send_all(MESSAGES, dry_run=True, origination_number_id="")
    assert len(delivery.logged) == 3


def test_dry_run_never_constructs_a_real_client(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("dry run must not create a boto3 client")

    monkeypatch.setattr("boto3.client", explode)
    send_all(MESSAGES, dry_run=True, origination_number_id=NUMBER)


# --- real sending -------------------------------------------------------------


def test_one_message_per_receiver():
    client = StubSms()
    delivery = send_all(
        MESSAGES, dry_run=False, origination_number_id=NUMBER, client=client
    )

    assert len(client.calls) == 3
    assert sorted(delivery.sent) == sorted(MESSAGES)
    assert delivery.failed == []


def test_every_send_is_pinned_to_the_configured_number():
    """Pinning the number, not a pool, keeps the sender number deterministic."""
    client = StubSms()
    send_all(MESSAGES, dry_run=False, origination_number_id=NUMBER, client=client)
    assert {c["OriginationIdentity"] for c in client.calls} == {NUMBER}


def test_each_receiver_gets_their_own_text():
    client = StubSms()
    send_all(MESSAGES, dry_run=False, origination_number_id=NUMBER, client=client)
    by_phone = {c["DestinationPhoneNumber"]: c["MessageBody"] for c in client.calls}
    assert by_phone == MESSAGES


def test_messages_are_transactional_not_promotional():
    """Promotional routing can be filtered or rate-limited by carriers."""
    client = StubSms()
    send_all(MESSAGES, dry_run=False, origination_number_id=NUMBER, client=client)
    assert {c["MessageType"] for c in client.calls} == {"TRANSACTIONAL"}


# --- partial failure ----------------------------------------------------------


def test_one_bad_number_does_not_stop_the_others():
    """Five roommates texted beats nobody texted."""
    client = StubSms(fail_for={"+15551230002"})
    delivery = send_all(
        MESSAGES, dry_run=False, origination_number_id=NUMBER, client=client
    )

    assert sorted(delivery.sent) == ["+15551230001", "+15551230003"]
    assert len(delivery.failed) == 1
    assert delivery.failed[0]["phone"] == "+15551230002"
    assert "carrier rejected" in delivery.failed[0]["error"]
    assert len(client.calls) == 3  # all three attempted


def test_every_number_failing_is_reported_not_raised():
    client = StubSms(fail_for=set(MESSAGES))
    delivery = send_all(
        MESSAGES, dry_run=False, origination_number_id=NUMBER, client=client
    )
    assert delivery.sent == []
    assert len(delivery.failed) == 3


# --- guardrails ---------------------------------------------------------------


def test_live_send_without_a_number_raises_clearly():
    with pytest.raises(ValueError, match="origination_number_id is required"):
        send_all(MESSAGES, dry_run=False, origination_number_id="", client=StubSms())


def test_empty_message_set_is_a_no_op():
    client = StubSms()
    delivery = send_all({}, dry_run=False, origination_number_id=NUMBER, client=client)
    assert client.calls == []
    assert delivery.sent == []


def test_delivery_dry_run_flag_is_false_for_real_sends():
    client = StubSms()
    delivery = send_all(
        MESSAGES, dry_run=False, origination_number_id=NUMBER, client=client
    )
    assert not delivery.dry_run


def test_empty_delivery_is_not_reported_as_dry_run():
    assert not Delivery().dry_run
