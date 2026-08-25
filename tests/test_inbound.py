"""Tests for inbound SMS handling: PAID, STATUS, and everything ignored."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from runtime_config import RuntimeConfig
from store import bill_pk, load_ledger, load_receivers, tally, write_ledger
from tests.conftest import load_handler
from tests.test_store import seed_config

inbound = load_handler("inbound")

BILL = bill_pk("pg-e", "2026-08")
GUS = "+15551230001"
SAM = "+15551230002"
ALEX = "+15551230003"
PAYER = "+15551230000"

MESSAGES = {
    "bill": "{amount}",
    "paid_ack": "Thanks {label}! {paid_count} of {receiver_count} paid the {biller} bill.",
    "status": "{biller} {month}: {paid_count} of {receiver_count} paid.",
    "reminder": "{amount}",
    "payment_alert": "{label} paid {amount} for {biller}. {paid_count} of {receiver_count} in.",
    "venmo_note": "{biller} bill split",
}


def config(**overrides) -> RuntimeConfig:
    base = {
        "table_name": "bill-bot-test",
        "model_id": "m",
        "timezone": "UTC",
        "dry_run": True,
        "on_low_confidence": "send",
        "no_due_date_text": "unknown",
        "record_ttl_days": 400,
        "venmo_username": "gus",
        "zelle_contact": None,
        "origination_number_id": "phone-abc",
        "payer_label": "Me",
        "payer_phone": PAYER,
        "payer_share": 1.0,
        "notify_on_payment": False,
        "secret_arn": "arn:x",
        "messages": MESSAGES,
    }
    return RuntimeConfig(**{**base, **overrides})


class StubSms:
    def __init__(self):
        self.calls: list[dict] = []

    def send_text_message(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "m"}


@pytest.fixture
def billed(state_table):
    """A seeded config plus one unpaid bill for all three roommates."""
    seed_config(state_table)
    state_table.put_item(
        Item={
            "PK": BILL,
            "SK": "META",
            "month": "2026-08",
            "provider_id": "pg-e",
            "provider_name": "PG&E",
            "total": Decimal("100.00"),
            "status": "notified",
        }
    )
    write_ledger(
        state_table,
        pk=BILL,
        amounts={
            GUS: Decimal("25.00"),
            SAM: Decimal("25.00"),
            ALEX: Decimal("50.00"),
        },
        receivers=load_receivers(state_table),
        record_ttl_days=400,
    )
    return state_table


# --- keyword parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["PAID", "paid", "Paid", " paid ", "paid.", "PAID!", "paid thanks", "Paid :)"],
)
def test_paid_variants_are_recognized(text):
    assert inbound.parse_keyword(text) == "PAID"


@pytest.mark.parametrize("text", ["STATUS", "status", " Status? ", "status please"])
def test_status_variants_are_recognized(text):
    assert inbound.parse_keyword(text) == "STATUS"


@pytest.mark.parametrize(
    "text", ["", "   ", None, "what", "I already paid you last week", "PAIDD"]
)
def test_unrecognized_text_returns_none(text):
    assert inbound.parse_keyword(text) is None


# --- PAID ---------------------------------------------------------------------


def test_paid_flips_the_row_and_replies(billed):
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone=GUS, body="PAID", config=config(dry_run=False), sms_client=sms
    )

    assert result["action"] == "paid"
    assert result["changed"] is True
    assert (result["paid"], result["of"]) == (1, 3)

    rows = {r["SK"]: r for r in load_ledger(billed, BILL)}
    assert rows[f"PAY#{GUS}"]["paid"] is True
    assert rows[f"PAY#{SAM}"]["paid"] is False

    assert len(sms.calls) == 1
    assert sms.calls[0]["DestinationPhoneNumber"] == GUS
    assert sms.calls[0]["MessageBody"] == (
        "Thanks Gus! 1 of 3 paid the PG&E bill."
    )


def test_paid_is_idempotent(billed):
    """Texting PAID twice must not double-count."""
    sms = StubSms()
    first = inbound.handle_message(
        billed, phone=GUS, body="PAID", config=config(dry_run=False), sms_client=sms
    )
    second = inbound.handle_message(
        billed, phone=GUS, body="PAID", config=config(dry_run=False), sms_client=sms
    )

    assert first["changed"] is True
    assert second["changed"] is False
    assert tally(billed, BILL) == (1, 3)
    # Still acknowledged both times, so the roommate isn't left wondering.
    assert len(sms.calls) == 2


def test_each_roommate_paying_advances_the_tally(billed):
    sms = StubSms()
    for phone, expected in ((GUS, 1), (SAM, 2), (ALEX, 3)):
        result = inbound.handle_message(
            billed, phone=phone, body="PAID",
            config=config(dry_run=False), sms_client=sms,
        )
        assert result["paid"] == expected
    assert tally(billed, BILL) == (3, 3)


def test_reply_goes_only_to_the_sender(billed):
    """Nobody else needs to be told that one person paid."""
    sms = StubSms()
    inbound.handle_message(
        billed, phone=ALEX, body="PAID", config=config(dry_run=False), sms_client=sms
    )
    assert [c["DestinationPhoneNumber"] for c in sms.calls] == [ALEX]


def test_paid_applies_to_the_newest_unpaid_bill(billed):
    """With two bills open, PAID settles the most recent one."""
    older = bill_pk("pg-e", "2026-07")
    billed.put_item(
        Item={"PK": older, "SK": "META", "month": "2026-07",
              "provider_name": "PG&E", "total": Decimal("90.00")}
    )
    write_ledger(
        billed, pk=older, amounts={GUS: Decimal("22.50")},
        receivers=load_receivers(billed), record_ttl_days=400,
    )

    result = inbound.handle_message(
        billed, phone=GUS, body="PAID", config=config(), sms_client=StubSms()
    )
    assert result["bill"] == BILL  # 2026-08, the newer one


# --- STATUS -------------------------------------------------------------------


def test_status_reports_the_tally_without_changing_anything(billed):
    sms = StubSms()
    inbound.handle_message(
        billed, phone=SAM, body="PAID", config=config(dry_run=False), sms_client=sms
    )
    result = inbound.handle_message(
        billed, phone=GUS, body="STATUS", config=config(dry_run=False), sms_client=sms
    )

    assert result["action"] == "status"
    assert (result["paid"], result["of"]) == (1, 3)
    assert sms.calls[-1]["MessageBody"] == "PG&E 2026-08: 1 of 3 paid."
    # STATUS must not mark the asker paid.
    rows = {r["SK"]: r for r in load_ledger(billed, BILL)}
    assert rows[f"PAY#{GUS}"]["paid"] is False


# --- ignored messages ---------------------------------------------------------


@pytest.mark.parametrize("keyword", ["STOP", "stop", "HELP", "UNSTOP", "CANCEL"])
def test_carrier_reserved_keywords_are_ignored(billed, keyword):
    """AWS handles opt-out; shadowing these would break compliance."""
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone=GUS, body=keyword, config=config(), sms_client=sms
    )
    assert result == {"action": "ignored", "reason": "reserved keyword"}
    assert sms.calls == []


def test_unrecognized_message_is_ignored_silently(billed):
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone=GUS, body="thanks!", config=config(), sms_client=sms
    )
    assert result["action"] == "ignored"
    assert sms.calls == []


def test_message_from_an_unknown_number_is_ignored_without_replying(billed):
    """Replying would confirm the number is live to whoever is probing."""
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone="+15559998888", body="PAID", config=config(), sms_client=sms
    )
    assert result == {"action": "ignored", "reason": "unknown sender"}
    assert sms.calls == []


def test_paid_with_no_open_bills_is_ignored(state_table):
    seed_config(state_table)
    sms = StubSms()
    result = inbound.handle_message(
        state_table, phone=GUS, body="PAID", config=config(), sms_client=sms
    )
    assert result == {"action": "ignored", "reason": "no open bills"}
    assert sms.calls == []


def test_already_settled_bill_is_acknowledged_not_reopened(billed):
    """Everyone has paid, so a stray PAID gets the tally rather than silence."""
    sms = StubSms()
    for phone in (GUS, SAM, ALEX):
        inbound.handle_message(
            billed, phone=phone, body="PAID",
            config=config(dry_run=False), sms_client=sms,
        )
    assert tally(billed, BILL) == (3, 3)

    result = inbound.handle_message(
        billed, phone=GUS, body="PAID", config=config(dry_run=False), sms_client=sms
    )
    assert result["action"] == "paid"
    assert result["changed"] is False
    assert (result["paid"], result["of"]) == (3, 3)
    assert tally(billed, BILL) == (3, 3)


def test_status_works_after_everyone_has_paid(billed):
    sms = StubSms()
    for phone in (GUS, SAM, ALEX):
        inbound.handle_message(
            billed, phone=phone, body="PAID", config=config(), sms_client=sms
        )
    result = inbound.handle_message(
        billed, phone=GUS, body="STATUS", config=config(dry_run=False), sms_client=sms
    )
    assert result["action"] == "status"
    assert sms.calls[-1]["MessageBody"] == "PG&E 2026-08: 3 of 3 paid."


# --- dry run ------------------------------------------------------------------


def test_dry_run_updates_the_ledger_but_sends_no_reply(billed):
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone=GUS, body="PAID", config=config(dry_run=True), sms_client=sms
    )
    assert result["changed"] is True
    assert tally(billed, BILL) == (1, 3)
    assert sms.calls == []


# --- alerting the payer -------------------------------------------------------


def test_payer_is_texted_when_someone_pays(billed):
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone=GUS, body="PAID",
        config=config(dry_run=False, notify_on_payment=True), sms_client=sms,
    )

    assert result["payerAlerted"] is True
    by_phone = {c["DestinationPhoneNumber"]: c["MessageBody"] for c in sms.calls}
    assert set(by_phone) == {GUS, PAYER}
    assert by_phone[GUS].startswith("Thanks Gus!")
    assert by_phone[PAYER] == "Gus paid $25.00 for PG&E. 1 of 3 in."


def test_payer_is_not_texted_when_the_option_is_off(billed):
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone=GUS, body="PAID",
        config=config(dry_run=False, notify_on_payment=False), sms_client=sms,
    )
    assert result["payerAlerted"] is False
    assert {c["DestinationPhoneNumber"] for c in sms.calls} == {GUS}


def test_payer_is_not_texted_without_a_phone_number(billed):
    """notify_on_payment can't fire with nowhere to send."""
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone=GUS, body="PAID",
        config=config(dry_run=False, notify_on_payment=True, payer_phone=None),
        sms_client=sms,
    )
    assert result["payerAlerted"] is False
    assert {c["DestinationPhoneNumber"] for c in sms.calls} == {GUS}


def test_payer_is_not_buzzed_twice_by_a_repeated_paid(billed):
    """Only a real state change is worth telling the payer about."""
    sms = StubSms()
    cfg = config(dry_run=False, notify_on_payment=True)
    first = inbound.handle_message(
        billed, phone=GUS, body="PAID", config=cfg, sms_client=sms
    )
    second = inbound.handle_message(
        billed, phone=GUS, body="PAID", config=cfg, sms_client=sms
    )

    assert first["payerAlerted"] is True
    assert second["payerAlerted"] is False
    assert [c["DestinationPhoneNumber"] for c in sms.calls].count(PAYER) == 1


def test_status_does_not_alert_the_payer(billed):
    sms = StubSms()
    inbound.handle_message(
        billed, phone=GUS, body="STATUS",
        config=config(dry_run=False, notify_on_payment=True), sms_client=sms,
    )
    assert {c["DestinationPhoneNumber"] for c in sms.calls} == {GUS}


def test_payer_alert_is_suppressed_in_dry_run(billed):
    sms = StubSms()
    result = inbound.handle_message(
        billed, phone=GUS, body="PAID",
        config=config(dry_run=True, notify_on_payment=True), sms_client=sms,
    )
    assert result["payerAlerted"] is True  # would have been sent
    assert sms.calls == []


# --- SNS envelope -------------------------------------------------------------


def sns_event(*messages) -> dict:
    return {
        "Records": [
            {"Sns": {"Message": json.dumps(m)}} for m in messages
        ]
    }


def test_handler_processes_an_sns_record(billed, monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "bill-bot-test")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    monkeypatch.setenv("ORIGINATION_NUMBER_ID", "phone-abc")

    result = inbound.handler(
        sns_event({"originationNumber": GUS, "messageBody": "PAID"}), None
    )
    assert result["handled"] == 1
    assert result["results"][0]["action"] == "paid"
    assert tally(billed, BILL) == (1, 3)


def test_handler_processes_a_batch(billed, monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "bill-bot-test")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))

    result = inbound.handler(
        sns_event(
            {"originationNumber": GUS, "messageBody": "PAID"},
            {"originationNumber": SAM, "messageBody": "PAID"},
        ),
        None,
    )
    assert result["handled"] == 2
    assert tally(billed, BILL) == (2, 3)


def test_malformed_sns_record_is_skipped(billed, monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "bill-bot-test")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))

    result = inbound.handler({"Records": [{"Sns": {"Message": "{not json"}}]}, None)
    assert result["handled"] == 0


def test_record_without_origination_number_is_skipped(billed, monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "bill-bot-test")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))

    result = inbound.handler(sns_event({"messageBody": "PAID"}), None)
    assert result["handled"] == 0


def test_one_failing_message_does_not_abort_the_batch(billed, monkeypatch):
    """Raising would make SNS redeliver and re-acknowledge everyone else."""
    monkeypatch.setenv("TABLE_NAME", "bill-bot-test")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))

    calls = {"n": 0}
    real = inbound.handle_message

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(inbound, "handle_message", flaky)
    result = inbound.handler(
        sns_event(
            {"originationNumber": GUS, "messageBody": "PAID"},
            {"originationNumber": SAM, "messageBody": "PAID"},
        ),
        None,
    )
    assert result["handled"] == 2
    assert result["results"][0]["action"] == "error"
    assert result["results"][1]["action"] == "paid"
