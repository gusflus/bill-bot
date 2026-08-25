"""DynamoDB access and the dedup decision.

The dedup rules are the reason this module exists as something separate and
testable. Restating them, because they're the fiddly part:

- A bill is keyed ``BILL#<provider>#<YYYY-MM>``, written with a conditional put
  on ``attribute_not_exists(PK)``. First writer wins, so a duplicate or
  concurrent invocation cannot produce two notifications.
- Seeing the same Gmail message id twice is always a replay, never a new bill.
- "Your bill is coming soon" then "your bill is due" is the common double-send.
  Same amount means the second one is redundant. A different amount means the
  ``bill_due`` figure wins, the record is updated, and a correction goes out.
- Because the bucket is the received month, a pair straddling a month boundary
  (soon on Aug 31, due on Sep 1) would otherwise land in different buckets and
  notify twice. Creating a bill therefore also checks the previous month for a
  matching amount.

Haiku classifies; this module decides. Dedup stays deterministic on purpose - a
re-run must never reach a different conclusion than the first run did.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

CONFIG_PK = "CONFIG"
RECEIVER_PREFIX = "RECEIVER#"
SENDER_PREFIX = "SENDER#"
PAY_PREFIX = "PAY#"
META_SK = "META"

STATUS_NOTIFIED = "notified"
STATUS_HELD = "held"

# How recently the previous month's bill must have been created for a
# same-amount bill this month to count as the same bill straddling a month
# boundary rather than a genuinely new one.
#
# This window is what makes the straddle guard safe for flat-rate senders. A
# fixed-price internet bill is the identical amount every month, so matching on
# amount alone would suppress every bill after the first. A real straddle pair
# ("coming soon" Aug 31, "due" Sep 1) lands days apart; next month's bill lands
# about thirty days later.
STRADDLE_WINDOW_DAYS = 5


class Decision(str, Enum):
    """What to do with an incoming bill email."""

    NEW = "new"              # first time seeing this bill - notify
    CORRECTED = "corrected"  # amount changed - update and notify
    DUPLICATE = "duplicate"  # already handled - stay quiet
    REPLAY = "replay"        # same email again - stay quiet


@dataclass(frozen=True)
class Receiver:
    label: str
    phone: str
    share: Decimal

    @property
    def weight(self) -> float:
        return float(self.share)


@dataclass(frozen=True)
class Sender:
    id: str
    name: str
    from_address: str


@dataclass(frozen=True)
class BillOutcome:
    decision: Decision
    bill_pk: str
    month: str
    previous_amount: Decimal | None = None

    @property
    def should_notify(self) -> bool:
        return self.decision in (Decision.NEW, Decision.CORRECTED)


def bill_pk(provider_id: str, month: str) -> str:
    return f"BILL#{provider_id}#{month}"


def previous_month(month: str) -> str:
    """'2026-01' -> '2025-12'."""
    year, mon = (int(part) for part in month.split("-"))
    return f"{year - 1:04d}-12" if mon == 1 else f"{year:04d}-{mon - 1:02d}"


# --- config reads -------------------------------------------------------------


def _config_rows(table, prefix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(CONFIG_PK)
        & Key("SK").begins_with(prefix)
    }
    while True:
        response = table.query(**kwargs)
        rows.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return rows
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def load_receivers(table) -> list[Receiver]:
    """Active receivers, ordered by phone so cent allocation is reproducible."""
    rows = _config_rows(table, RECEIVER_PREFIX)
    receivers = [
        Receiver(label=r["label"], phone=r["phone"], share=r["share"])
        for r in rows
        if r.get("active", True)
    ]
    return sorted(receivers, key=lambda r: r.phone)


def load_senders(table) -> list[Sender]:
    return [
        Sender(id=s["id"], name=s["name"], from_address=s["from_address"])
        for s in _config_rows(table, SENDER_PREFIX)
    ]


def find_sender(senders: list[Sender], from_header: str) -> Sender | None:
    """Match a raw From header against configured senders.

    Configured addresses are usually bare domains ('socalgas.com'), while the
    header is 'SoCalGas <no-reply@email.socalgas.com>'. Longest match wins so a
    specific address beats a domain if both are configured.
    """
    haystack = (from_header or "").lower()
    matches = [s for s in senders if s.from_address.lower() in haystack]
    if not matches:
        return None
    return max(matches, key=lambda s: len(s.from_address))


# --- bill writes --------------------------------------------------------------


def _ttl(record_ttl_days: int) -> int:
    return int(time.time()) + record_ttl_days * 86400


def get_bill(table, pk: str) -> dict[str, Any] | None:
    return table.get_item(Key={"PK": pk, "SK": META_SK}).get("Item")


def _bill_item(
    *,
    pk: str,
    month: str,
    provider_id: str,
    provider_name: str,
    total: Decimal,
    due_date: date | None,
    classification: str,
    confidence: str,
    status: str,
    thread_id: str,
    message_id: str,
    record_ttl_days: int,
) -> dict[str, Any]:
    return {
        "PK": pk,
        "SK": META_SK,
        "month": month,
        "provider_id": provider_id,
        "provider_name": provider_name,
        "total": total,
        "due_date": due_date.isoformat() if due_date else None,
        "classification": classification,
        "confidence": confidence,
        "status": status,
        "thread_id": thread_id,
        "source_message_ids": {message_id},
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "ttl": _ttl(record_ttl_days),
    }


def record_bill(
    table,
    *,
    provider_id: str,
    provider_name: str,
    month: str,
    total: Decimal,
    due_date: date | None,
    classification: str,
    confidence: str,
    status: str,
    thread_id: str,
    message_id: str,
    record_ttl_days: int,
) -> BillOutcome:
    """Create or reconcile a bill record, returning what should happen next."""
    pk = bill_pk(provider_id, month)

    existing = get_bill(table, pk)
    if existing is not None:
        return _reconcile(
            table,
            existing=existing,
            pk=pk,
            month=month,
            total=total,
            due_date=due_date,
            classification=classification,
            confidence=confidence,
            message_id=message_id,
        )

    # A pair of emails straddling a month boundary belongs to one bill - but
    # only if the earlier one is recent. See STRADDLE_WINDOW_DAYS.
    straddle = get_bill(table, bill_pk(provider_id, previous_month(month)))
    if straddle is not None and _is_straddle(straddle, total):
        logger.info(
            "amount %s was recorded in %s %d day(s) ago; treating as the same bill",
            total,
            straddle["PK"],
            (int(time.time()) - int(straddle.get("created_at", 0))) // 86400,
        )
        return _reconcile(
            table,
            existing=straddle,
            pk=straddle["PK"],
            month=straddle["month"],
            total=total,
            due_date=due_date,
            classification=classification,
            confidence=confidence,
            message_id=message_id,
        )

    item = _bill_item(
        pk=pk,
        month=month,
        provider_id=provider_id,
        provider_name=provider_name,
        total=total,
        due_date=due_date,
        classification=classification,
        confidence=confidence,
        status=status,
        thread_id=thread_id,
        message_id=message_id,
        record_ttl_days=record_ttl_days,
    )
    try:
        table.put_item(
            Item=item, ConditionExpression="attribute_not_exists(PK)"
        )
    except Exception as exc:  # noqa: BLE001 - ConditionalCheckFailedException
        if "ConditionalCheckFailed" not in type(exc).__name__ and "ConditionalCheckFailed" not in str(exc):
            raise
        # Another invocation won the race; re-read and reconcile against it.
        logger.info("lost the create race for %s, reconciling instead", pk)
        current = get_bill(table, pk)
        if current is None:  # pragma: no cover - vanishingly unlikely
            raise
        return _reconcile(
            table,
            existing=current,
            pk=pk,
            month=month,
            total=total,
            due_date=due_date,
            classification=classification,
            confidence=confidence,
            message_id=message_id,
        )

    logger.info("recorded new bill %s total=%s status=%s", pk, total, status)
    return BillOutcome(decision=Decision.NEW, bill_pk=pk, month=month)


def _is_straddle(candidate: dict[str, Any], total: Decimal) -> bool:
    """Whether last month's bill is really this same bill, seen again.

    Requires both the same amount and a recent creation time. Amount alone
    would break flat-rate senders whose bill never changes.
    """
    if candidate.get("total") != total:
        return False
    created_at = int(candidate.get("created_at", 0))
    age_seconds = int(time.time()) - created_at
    return 0 <= age_seconds <= STRADDLE_WINDOW_DAYS * 86400


def _reconcile(
    table,
    *,
    existing: dict[str, Any],
    pk: str,
    month: str,
    total: Decimal,
    due_date: date | None,
    classification: str,
    confidence: str,
    message_id: str,
) -> BillOutcome:
    """Decide what a second email for an already-recorded bill means."""
    seen = existing.get("source_message_ids") or set()
    if message_id in seen:
        logger.info("message %s already processed for %s", message_id, pk)
        return BillOutcome(decision=Decision.REPLAY, bill_pk=pk, month=month)

    _append_message_id(table, pk, message_id)
    previous_total = existing.get("total")

    if previous_total == total:
        # The "coming soon" / "due" pair for the same amount. Fill in a due date
        # if this email finally supplied one, but don't text anyone again.
        if due_date and not existing.get("due_date"):
            _set_due_date(table, pk, due_date)
        logger.info("duplicate bill email for %s (total unchanged)", pk)
        return BillOutcome(decision=Decision.DUPLICATE, bill_pk=pk, month=month)

    logger.info(
        "bill %s amount changed %s -> %s; sending a correction",
        pk,
        previous_total,
        total,
    )
    table.update_item(
        Key={"PK": pk, "SK": META_SK},
        UpdateExpression=(
            "SET #total = :total, due_date = :due, classification = :cls, "
            "confidence = :conf, updated_at = :now, corrected_from = :prev"
        ),
        ExpressionAttributeNames={"#total": "total"},
        ExpressionAttributeValues={
            ":total": total,
            ":due": due_date.isoformat() if due_date else existing.get("due_date"),
            ":cls": classification,
            ":conf": confidence,
            ":now": int(time.time()),
            ":prev": previous_total,
        },
    )
    return BillOutcome(
        decision=Decision.CORRECTED,
        bill_pk=pk,
        month=month,
        previous_amount=previous_total,
    )


def _append_message_id(table, pk: str, message_id: str) -> None:
    table.update_item(
        Key={"PK": pk, "SK": META_SK},
        UpdateExpression="ADD source_message_ids :mid",
        ExpressionAttributeValues={":mid": {message_id}},
    )


def _set_due_date(table, pk: str, due_date: date) -> None:
    table.update_item(
        Key={"PK": pk, "SK": META_SK},
        UpdateExpression="SET due_date = :due, updated_at = :now",
        ExpressionAttributeValues={
            ":due": due_date.isoformat(),
            ":now": int(time.time()),
        },
    )


def set_bill_status(table, pk: str, status: str) -> None:
    table.update_item(
        Key={"PK": pk, "SK": META_SK},
        UpdateExpression="SET #s = :s, updated_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":now": int(time.time())},
    )


# --- payment ledger -----------------------------------------------------------


def write_ledger(
    table,
    *,
    pk: str,
    amounts: dict[str, Decimal],
    receivers: list[Receiver],
    record_ttl_days: int,
    payer_amount: Decimal | None = None,
) -> None:
    """Create one unpaid row per receiver. Overwrites on a correction.

    The payer gets no row - there is nothing to collect from the person who paid
    the utility. Their absorbed portion is recorded on the bill instead, so
    ``sum(ledger) + payer_amount`` still reconciles to the total.
    """
    by_phone = {r.phone: r for r in receivers}
    with table.batch_writer() as batch:
        for phone, amount in amounts.items():
            batch.put_item(
                Item={
                    "PK": pk,
                    "SK": f"{PAY_PREFIX}{phone}",
                    "phone": phone,
                    "label": by_phone[phone].label,
                    "amount_owed": amount,
                    "paid": False,
                    "paid_at": None,
                    "reminded_at": None,
                    "ttl": _ttl(record_ttl_days),
                }
            )

    if payer_amount is not None:
        table.update_item(
            Key={"PK": pk, "SK": META_SK},
            UpdateExpression="SET payer_amount = :p, updated_at = :now",
            ExpressionAttributeValues={
                ":p": payer_amount,
                ":now": int(time.time()),
            },
        )


def load_ledger(table, pk: str) -> list[dict[str, Any]]:
    response = table.query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(PAY_PREFIX)
    )
    return sorted(response.get("Items", []), key=lambda i: i["SK"])


def mark_paid(table, pk: str, phone: str) -> bool:
    """Flip one ledger row to paid. Returns False if it was already paid.

    Idempotent, so a roommate texting PAID twice doesn't double-count.
    """
    try:
        table.update_item(
            Key={"PK": pk, "SK": f"{PAY_PREFIX}{phone}"},
            UpdateExpression="SET paid = :true, paid_at = :now",
            ConditionExpression="attribute_exists(SK) AND paid = :false",
            ExpressionAttributeValues={
                ":true": True,
                ":false": False,
                ":now": int(time.time()),
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - ConditionalCheckFailedException
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            return False
        raise


def tally(table, pk: str) -> tuple[int, int]:
    """(paid, total) for one bill."""
    rows = load_ledger(table, pk)
    return sum(1 for r in rows if r.get("paid")), len(rows)


def find_bills_for(table, phone: str, *, unpaid_only: bool) -> list[dict[str, Any]]:
    """Ledger rows belonging to ``phone``, newest bill first.

    A Scan is fine here: this table holds a few dozen bills a year, so an index
    would cost more in complexity than it saves in read units. Revisit with a
    status GSI if the table ever grows past a few thousand items.
    """
    rows: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": Key("SK").eq(f"{PAY_PREFIX}{phone}"),
        "ProjectionExpression": "PK, SK, paid, amount_owed, #l",
        "ExpressionAttributeNames": {"#l": "label"},
    }
    while True:
        response = table.scan(**kwargs)
        rows.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    if unpaid_only:
        rows = [r for r in rows if not r.get("paid")]
    return sorted(rows, key=lambda i: i["PK"], reverse=True)


def find_open_bills(table, phone: str) -> list[dict[str, Any]]:
    """Bills with an unpaid row for ``phone``, newest first."""
    return find_bills_for(table, phone, unpaid_only=True)
