"""Tests for unpaid reminders: who gets nudged, when, and who never does."""

from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from runtime_config import RuntimeConfig
from store import bill_pk, load_receivers, mark_paid, write_ledger
from tests.conftest import load_handler
from tests.test_store import seed_config

reminders = load_handler("reminders")

BILL = bill_pk("pg-e", "2026-08")
GUS = "+15551230001"
SAM = "+15551230002"
ALEX = "+15551230003"
DAY = 86400

MESSAGES = {
    "bill": "{amount}",
    "paid_ack": "{paid_count}",
    "status": "{paid_count}",
    "reminder": "Reminder: {amount} still owed for the {biller} bill. {venmo_link}",
    "payment_alert": "{label} paid {amount}.",
    "venmo_note": "{biller} bill split",
}


def config(**overrides) -> RuntimeConfig:
    base = {
        "table_name": "bill-bot-test",
        "model_id": "m",
        "timezone": "UTC",
        "dry_run": False,
        "on_low_confidence": "send",
        "no_due_date_text": "unknown",
        "record_ttl_days": 400,
        "venmo_username": "gus",
        "zelle_contact": None,
        "origination_number_id": "phone-abc",
        "payer_share": 0.0,
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
    """A bill created 10 days ago, unpaid by everyone."""
    seed_config(state_table)
    state_table.put_item(
        Item={
            "PK": BILL,
            "SK": "META",
            "month": "2026-08",
            "provider_name": "PG&E",
            "total": Decimal("100.00"),
            "created_at": int(time.time()) - 10 * DAY,
        }
    )
    write_ledger(
        state_table,
        pk=BILL,
        amounts={GUS: Decimal("25.00"), SAM: Decimal("25.00"), ALEX: Decimal("50.00")},
        receivers=load_receivers(state_table),
        record_ttl_days=400,
    )
    return state_table


def texted(sms) -> set[str]:
    return {c["DestinationPhoneNumber"] for c in sms.calls}


# --- who gets nudged ----------------------------------------------------------


def test_all_unpaid_roommates_are_reminded(billed):
    sms = StubSms()
    result = reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=sms
    )
    assert result == {"bills": 1, "reminded": 3}
    assert texted(sms) == {GUS, SAM, ALEX}


def test_paid_roommates_are_never_reminded(billed):
    """The whole point: settle up and the bot leaves you alone."""
    mark_paid(billed, BILL, GUS)
    sms = StubSms()
    result = reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=sms
    )
    assert result["reminded"] == 2
    assert texted(sms) == {SAM, ALEX}


def test_fully_paid_bill_produces_nothing(billed):
    for phone in (GUS, SAM, ALEX):
        mark_paid(billed, BILL, phone)
    sms = StubSms()
    result = reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=sms
    )
    assert result == {"bills": 0, "reminded": 0}
    assert sms.calls == []


def test_reminder_carries_the_persons_own_amount(billed):
    sms = StubSms()
    reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=sms
    )
    by_phone = {c["DestinationPhoneNumber"]: c["MessageBody"] for c in sms.calls}
    assert "$25.00" in by_phone[GUS]
    assert "$50.00" in by_phone[ALEX]
    assert "PG&E" in by_phone[ALEX]
    assert "venmo.com" in by_phone[ALEX]


# --- timing -------------------------------------------------------------------


def test_bill_younger_than_after_days_is_left_alone(state_table):
    seed_config(state_table)
    state_table.put_item(
        Item={
            "PK": BILL,
            "SK": "META",
            "provider_name": "PG&E",
            "month": "2026-08",
            "total": Decimal("100.00"),
            "created_at": int(time.time()) - 1 * DAY,
        }
    )
    write_ledger(
        state_table, pk=BILL, amounts={GUS: Decimal("25.00")},
        receivers=load_receivers(state_table), record_ttl_days=400,
    )

    sms = StubSms()
    result = reminders.send_reminders(
        state_table, config(), after_days=3, repeat_days=3, sms_client=sms
    )
    assert result == {"bills": 0, "reminded": 0}
    assert sms.calls == []


def test_second_run_the_same_day_does_not_nag_again(billed):
    sms = StubSms()
    first = reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=sms
    )
    second = reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=sms
    )
    assert first["reminded"] == 3
    assert second == {"bills": 0, "reminded": 0}
    assert len(sms.calls) == 3


def test_reminder_repeats_once_the_window_has_passed(billed):
    sms = StubSms()
    now = int(time.time())
    reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, now=now, sms_client=sms
    )
    later = reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3,
        now=now + 4 * DAY, sms_client=sms,
    )
    assert later["reminded"] == 3
    assert len(sms.calls) == 6


def test_reminded_at_is_recorded(billed):
    reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=StubSms()
    )
    row = billed.get_item(Key={"PK": BILL, "SK": f"PAY#{GUS}"})["Item"]
    assert row["reminded_at"] is not None


# --- due_for_reminder predicate ------------------------------------------------


def test_predicate_ignores_paid_rows():
    now = int(time.time())
    bill = {"created_at": now - 10 * DAY}
    assert not reminders.due_for_reminder(
        {"paid": True}, bill, now=now, after_days=3, repeat_days=3
    )


def test_predicate_ignores_bills_without_a_created_at():
    now = int(time.time())
    assert not reminders.due_for_reminder(
        {"paid": False}, {}, now=now, after_days=3, repeat_days=3
    )


def test_predicate_respects_the_repeat_window():
    now = int(time.time())
    bill = {"created_at": now - 10 * DAY}
    row = {"paid": False, "reminded_at": now - 1 * DAY}
    assert not reminders.due_for_reminder(
        row, bill, now=now, after_days=3, repeat_days=3
    )
    assert reminders.due_for_reminder(
        row, bill, now=now + 3 * DAY, after_days=3, repeat_days=3
    )


# --- edge cases ---------------------------------------------------------------


def test_receiver_removed_from_config_is_skipped(billed):
    """Someone who moved out shouldn't get texted about an old bill."""
    billed.delete_item(Key={"PK": "CONFIG", "SK": f"RECEIVER#{ALEX}"})
    sms = StubSms()
    result = reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=sms
    )
    assert result["reminded"] == 2
    assert ALEX not in texted(sms)


def test_dry_run_sends_nothing_but_still_records(billed):
    sms = StubSms()
    result = reminders.send_reminders(
        billed, config(dry_run=True), after_days=3, repeat_days=3, sms_client=sms
    )
    assert result["reminded"] == 3
    assert sms.calls == []


def test_multiple_bills_are_each_handled(billed):
    older = bill_pk("socalgas", "2026-08")
    billed.put_item(
        Item={
            "PK": older,
            "SK": "META",
            "month": "2026-08",
            "provider_name": "SoCalGas",
            "total": Decimal("44.05"),
            "created_at": int(time.time()) - 10 * DAY,
        }
    )
    write_ledger(
        billed, pk=older, amounts={GUS: Decimal("11.01")},
        receivers=load_receivers(billed), record_ttl_days=400,
    )

    result = reminders.send_reminders(
        billed, config(), after_days=3, repeat_days=3, sms_client=StubSms()
    )
    assert result == {"bills": 2, "reminded": 4}


def test_handler_reads_thresholds_from_the_environment(billed, monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "bill-bot-test")
    monkeypatch.setenv("MESSAGES", json.dumps(MESSAGES))
    monkeypatch.setenv("REMINDER_AFTER_DAYS", "30")
    monkeypatch.setenv("DRY_RUN", "true")

    # Bill is 10 days old, threshold is 30 - nothing should fire.
    assert reminders.handler({}, None) == {"bills": 0, "reminded": 0}
