"""Tests for the dedup decision and the payment ledger.

These are the rules that keep a roommate from being charged twice for one bill,
so they get exercised hard.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from store import (
    STRADDLE_WINDOW_DAYS,
    Decision,
    Receiver,
    Sender,
    bill_pk,
    find_open_bills,
    find_sender,
    get_bill,
    load_ledger,
    load_receivers,
    load_senders,
    mark_paid,
    previous_month,
    record_bill,
    tally,
    write_ledger,
)

TTL_DAYS = 400


def seed_config(table, receivers=None, senders=None):
    receivers = receivers if receivers is not None else [
        ("Gus", "+15551230001", 1),
        ("Sam", "+15551230002", 1),
        ("Alex", "+15551230003", 2),
    ]
    senders = senders if senders is not None else [
        ("pg-e", "PG&E", "billpay.pge.com"),
        ("socalgas", "SoCalGas", "socalgas.com"),
    ]
    for label, phone, share in receivers:
        table.put_item(
            Item={
                "PK": "CONFIG",
                "SK": f"RECEIVER#{phone}",
                "phone": phone,
                "label": label,
                "share": Decimal(str(share)),
                "active": True,
            }
        )
    for sid, name, addr in senders:
        table.put_item(
            Item={
                "PK": "CONFIG",
                "SK": f"SENDER#{addr}",
                "id": sid,
                "name": name,
                "from_address": addr,
            }
        )


def post_bill(table, *, total="142.53", month="2026-08", message_id="msg-1",
              classification="bill_due", due_date=None, provider="pg-e"):
    return record_bill(
        table,
        provider_id=provider,
        provider_name="PG&E",
        month=month,
        total=Decimal(total),
        due_date=due_date,
        classification=classification,
        confidence="high",
        status="notified",
        thread_id="thread-1",
        message_id=message_id,
        record_ttl_days=TTL_DAYS,
    )


def age_bill(table, pk: str, *, days: int) -> None:
    """Backdate a bill's created_at, to test the straddle recency window."""
    import time

    table.update_item(
        Key={"PK": pk, "SK": "META"},
        UpdateExpression="SET created_at = :t",
        ExpressionAttributeValues={":t": int(time.time()) - days * 86400},
    )


# --- config reads -------------------------------------------------------------


def test_receivers_load_sorted_by_phone(state_table):
    """Stable ordering keeps largest-remainder cent allocation reproducible."""
    seed_config(state_table)
    receivers = load_receivers(state_table)
    assert [r.phone for r in receivers] == [
        "+15551230001",
        "+15551230002",
        "+15551230003",
    ]
    assert receivers[2].weight == 2.0


def test_inactive_receivers_are_skipped(state_table):
    seed_config(state_table)
    state_table.update_item(
        Key={"PK": "CONFIG", "SK": "RECEIVER#+15551230002"},
        UpdateExpression="SET active = :f",
        ExpressionAttributeValues={":f": False},
    )
    assert len(load_receivers(state_table)) == 2


def test_senders_load(state_table):
    seed_config(state_table)
    assert {s.id for s in load_senders(state_table)} == {"pg-e", "socalgas"}


# --- sender matching ----------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "billpay.pge.com",
        "PG&E <no-reply@billpay.pge.com>",
        "\"PG&E Billing\" <DoNotReply@BILLPAY.PGE.COM>",
    ],
)
def test_from_header_variants_match(header):
    senders = [Sender("pg-e", "PG&E", "billpay.pge.com")]
    assert find_sender(senders, header).id == "pg-e"


def test_unmatched_header_returns_none():
    senders = [Sender("pg-e", "PG&E", "billpay.pge.com")]
    assert find_sender(senders, "spam <hi@example.com>") is None
    assert find_sender(senders, "") is None
    assert find_sender([], "billpay.pge.com") is None


def test_most_specific_sender_wins():
    """A full address beats a bare domain if both are configured."""
    senders = [
        Sender("generic", "Generic", "pge.com"),
        Sender("billing", "Billing", "billpay.pge.com"),
    ]
    assert find_sender(senders, "x@billpay.pge.com").id == "billing"


# --- first write --------------------------------------------------------------


def test_first_bill_is_new(state_table):
    outcome = post_bill(state_table)
    assert outcome.decision == Decision.NEW
    assert outcome.should_notify
    assert outcome.bill_pk == "BILL#pg-e#2026-08"

    item = get_bill(state_table, outcome.bill_pk)
    assert item["total"] == Decimal("142.53")
    assert item["source_message_ids"] == {"msg-1"}
    assert item["ttl"] > item["created_at"]


def test_different_providers_same_month_are_separate_bills(state_table):
    assert post_bill(state_table, provider="pg-e").decision == Decision.NEW
    assert post_bill(
        state_table, provider="socalgas", message_id="msg-2", total="44.05"
    ).decision == Decision.NEW


def test_same_provider_different_months_are_separate_bills(state_table):
    """A month apart, so not a straddle even at the identical amount."""
    assert post_bill(state_table, month="2026-08").decision == Decision.NEW
    age_bill(state_table, "BILL#pg-e#2026-08", days=30)
    assert post_bill(
        state_table, month="2026-09", message_id="msg-2"
    ).decision == Decision.NEW


def test_flat_rate_sender_bills_every_month(state_table):
    """Spectrum charges the same amount forever - each month is its own bill.

    Matching on amount alone would have suppressed every bill after the first.
    """
    for index, month in enumerate(["2026-06", "2026-07", "2026-08", "2026-09"]):
        outcome = post_bill(
            state_table,
            provider="spectrum",
            month=month,
            total="89.99",
            message_id=f"spectrum-{index}",
        )
        assert outcome.decision == Decision.NEW, f"{month} should be its own bill"
        age_bill(state_table, outcome.bill_pk, days=30)


# --- replay ------------------------------------------------------------------


def test_same_message_id_is_a_replay(state_table):
    post_bill(state_table, message_id="msg-1")
    outcome = post_bill(state_table, message_id="msg-1")
    assert outcome.decision == Decision.REPLAY
    assert not outcome.should_notify


def test_replay_detected_even_when_amount_differs(state_table):
    """The message id is checked before the amount - same email, same answer."""
    post_bill(state_table, message_id="msg-1", total="142.53")
    outcome = post_bill(state_table, message_id="msg-1", total="999.99")
    assert outcome.decision == Decision.REPLAY
    assert get_bill(state_table, "BILL#pg-e#2026-08")["total"] == Decimal("142.53")


# --- the coming-soon / due pair ------------------------------------------------


def test_upcoming_then_due_same_amount_is_a_duplicate(state_table):
    """The exact scenario from the original TODO."""
    first = post_bill(
        state_table,
        message_id="soon",
        classification="bill_upcoming",
        total="142.53",
    )
    assert first.decision == Decision.NEW

    second = post_bill(
        state_table,
        message_id="due",
        classification="bill_due",
        total="142.53",
    )
    assert second.decision == Decision.DUPLICATE
    assert not second.should_notify

    # Both emails recorded against the one bill.
    assert get_bill(state_table, "BILL#pg-e#2026-08")["source_message_ids"] == {
        "soon",
        "due",
    }


def test_due_email_fills_in_a_missing_due_date(state_table):
    """The 'coming soon' notice rarely states one; the real bill does."""
    post_bill(state_table, message_id="soon", classification="bill_upcoming")
    assert get_bill(state_table, "BILL#pg-e#2026-08")["due_date"] is None

    post_bill(
        state_table,
        message_id="due",
        classification="bill_due",
        due_date=date(2026, 9, 8),
    )
    assert get_bill(state_table, "BILL#pg-e#2026-08")["due_date"] == "2026-09-08"


def test_changed_amount_produces_a_correction(state_table):
    post_bill(state_table, message_id="soon", total="140.00",
              classification="bill_upcoming")
    outcome = post_bill(state_table, message_id="due", total="142.53",
                        classification="bill_due")

    assert outcome.decision == Decision.CORRECTED
    assert outcome.should_notify
    assert outcome.previous_amount == Decimal("140.00")

    item = get_bill(state_table, "BILL#pg-e#2026-08")
    assert item["total"] == Decimal("142.53")
    assert item["corrected_from"] == Decimal("140.00")
    assert item["classification"] == "bill_due"


def test_third_email_after_a_correction_is_a_duplicate(state_table):
    post_bill(state_table, message_id="a", total="140.00")
    post_bill(state_table, message_id="b", total="142.53")
    assert post_bill(state_table, message_id="c", total="142.53").decision == (
        Decision.DUPLICATE
    )


# --- month boundary straddle --------------------------------------------------


def test_previous_month():
    assert previous_month("2026-08") == "2026-07"
    assert previous_month("2026-01") == "2025-12"
    assert previous_month("2026-10") == "2026-09"


def test_pair_straddling_a_month_boundary_is_one_bill(state_table):
    """Soon on Aug 31, due on Sep 1 - must not notify twice."""
    first = post_bill(state_table, month="2026-08", message_id="soon",
                      total="142.53", classification="bill_upcoming")
    assert first.decision == Decision.NEW

    second = post_bill(state_table, month="2026-09", message_id="due",
                       total="142.53", classification="bill_due")
    assert second.decision == Decision.DUPLICATE
    # Recorded against the original bucket, not a new one.
    assert second.bill_pk == "BILL#pg-e#2026-08"
    assert get_bill(state_table, "BILL#pg-e#2026-09") is None


def test_straddle_guard_only_fires_on_a_matching_amount(state_table):
    """Next month's genuinely different bill is still a new bill."""
    post_bill(state_table, month="2026-08", message_id="aug", total="142.53")
    outcome = post_bill(state_table, month="2026-09", message_id="sep",
                        total="98.10")
    assert outcome.decision == Decision.NEW
    assert outcome.bill_pk == "BILL#pg-e#2026-09"


def test_straddle_guard_expires_after_its_window(state_table):
    """Same amount but a month later is next month's bill, not a straddle."""
    post_bill(state_table, month="2026-08", message_id="aug", total="142.53")
    age_bill(state_table, "BILL#pg-e#2026-08", days=STRADDLE_WINDOW_DAYS + 1)

    outcome = post_bill(state_table, month="2026-09", message_id="sep",
                        total="142.53")
    assert outcome.decision == Decision.NEW
    assert outcome.bill_pk == "BILL#pg-e#2026-09"


def test_straddle_guard_does_not_reach_back_two_months(state_table):
    post_bill(state_table, month="2026-06", message_id="jun", total="142.53")
    outcome = post_bill(state_table, month="2026-08", message_id="aug",
                        total="142.53")
    assert outcome.decision == Decision.NEW


# --- ledger ------------------------------------------------------------------


def test_ledger_rows_start_unpaid(state_table):
    seed_config(state_table)
    receivers = load_receivers(state_table)
    pk = bill_pk("pg-e", "2026-08")
    write_ledger(
        state_table,
        pk=pk,
        amounts={
            "+15551230001": Decimal("35.63"),
            "+15551230002": Decimal("35.63"),
            "+15551230003": Decimal("71.27"),
        },
        receivers=receivers,
        record_ttl_days=TTL_DAYS,
    )

    rows = load_ledger(state_table, pk)
    assert len(rows) == 3
    assert all(r["paid"] is False for r in rows)
    assert {r["label"] for r in rows} == {"Gus", "Sam", "Alex"}
    assert sum(r["amount_owed"] for r in rows) == Decimal("142.53")


def test_mark_paid_flips_one_row(state_table):
    seed_config(state_table)
    pk = bill_pk("pg-e", "2026-08")
    write_ledger(
        state_table,
        pk=pk,
        amounts={"+15551230001": Decimal("10.00"), "+15551230002": Decimal("10.00")},
        receivers=load_receivers(state_table),
        record_ttl_days=TTL_DAYS,
    )

    assert mark_paid(state_table, pk, "+15551230001") is True
    assert tally(state_table, pk) == (1, 2)


def test_mark_paid_is_idempotent(state_table):
    """Texting PAID twice must not double-count."""
    seed_config(state_table)
    pk = bill_pk("pg-e", "2026-08")
    write_ledger(
        state_table,
        pk=pk,
        amounts={"+15551230001": Decimal("10.00")},
        receivers=load_receivers(state_table),
        record_ttl_days=TTL_DAYS,
    )

    assert mark_paid(state_table, pk, "+15551230001") is True
    assert mark_paid(state_table, pk, "+15551230001") is False
    assert tally(state_table, pk) == (1, 1)


def test_mark_paid_on_a_missing_row_returns_false(state_table):
    assert mark_paid(state_table, bill_pk("pg-e", "2026-08"), "+15559999999") is False


def test_find_open_bills_returns_only_unpaid_rows_for_that_phone(state_table):
    seed_config(state_table)
    receivers = load_receivers(state_table)
    for month in ("2026-07", "2026-08"):
        write_ledger(
            state_table,
            pk=bill_pk("pg-e", month),
            amounts={
                "+15551230001": Decimal("10.00"),
                "+15551230002": Decimal("10.00"),
            },
            receivers=receivers,
            record_ttl_days=TTL_DAYS,
        )
    mark_paid(state_table, bill_pk("pg-e", "2026-07"), "+15551230001")

    open_bills = find_open_bills(state_table, "+15551230001")
    assert [b["PK"] for b in open_bills] == ["BILL#pg-e#2026-08"]

    # Newest first, and unaffected by the other roommate's payments.
    assert len(find_open_bills(state_table, "+15551230002")) == 2
    assert find_open_bills(state_table, "+15551230002")[0]["PK"] == "BILL#pg-e#2026-08"


def test_correction_rewrites_ledger_amounts(state_table):
    seed_config(state_table)
    receivers = load_receivers(state_table)
    pk = bill_pk("pg-e", "2026-08")

    write_ledger(state_table, pk=pk, amounts={"+15551230001": Decimal("10.00")},
                 receivers=receivers, record_ttl_days=TTL_DAYS)
    write_ledger(state_table, pk=pk, amounts={"+15551230001": Decimal("25.00")},
                 receivers=receivers, record_ttl_days=TTL_DAYS)

    rows = load_ledger(state_table, pk)
    assert len(rows) == 1
    assert rows[0]["amount_owed"] == Decimal("25.00")
