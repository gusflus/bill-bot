"""Reminder Lambda: nudges roommates who still owe on a bill.

Runs on a schedule. For each bill with unpaid ledger rows older than
``reminders.after_days``, it texts only the people who haven't paid, and records
``reminded_at`` so the same person isn't nagged again until
``reminders.repeat_days`` has passed.

Only unpaid rows are ever contacted - someone who has already paid should never
hear about the bill again.
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any

import boto3

from render import bill_values, render
from runtime_config import RuntimeConfig
from sender import send_all
from store import PAY_PREFIX, load_receivers

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DAY_SECONDS = 86400


def _scan_all(table, **kwargs) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def due_for_reminder(
    row: dict[str, Any],
    bill: dict[str, Any],
    *,
    now: int,
    after_days: int,
    repeat_days: int,
) -> bool:
    """Whether this unpaid row has waited long enough to be nudged."""
    if row.get("paid"):
        return False

    created_at = int(bill.get("created_at", 0))
    if not created_at or now - created_at < after_days * DAY_SECONDS:
        return False

    last = row.get("reminded_at")
    if last and now - int(last) < repeat_days * DAY_SECONDS:
        return False
    return True


def collect_reminders(
    table, *, now: int, after_days: int, repeat_days: int
) -> dict[str, list[dict[str, Any]]]:
    """Group rows needing a reminder by bill PK."""
    rows = _scan_all(
        table,
        FilterExpression="begins_with(SK, :p)",
        ExpressionAttributeValues={":p": PAY_PREFIX},
    )
    bills = {
        item["PK"]: item
        for item in _scan_all(
            table,
            FilterExpression="SK = :meta",
            ExpressionAttributeValues={":meta": "META"},
        )
    }

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        bill = bills.get(row["PK"])
        if bill is None:
            continue
        if due_for_reminder(
            row, bill, now=now, after_days=after_days, repeat_days=repeat_days
        ):
            grouped.setdefault(row["PK"], []).append(row)
    return grouped


def _mark_reminded(table, pk: str, phone: str, now: int) -> None:
    table.update_item(
        Key={"PK": pk, "SK": f"{PAY_PREFIX}{phone}"},
        UpdateExpression="SET reminded_at = :now",
        ExpressionAttributeValues={":now": now},
    )


def send_reminders(
    table, config: RuntimeConfig, *, after_days: int, repeat_days: int,
    now: int | None = None, sms_client=None,
) -> dict[str, Any]:
    now = int(time.time()) if now is None else now
    grouped = collect_reminders(
        table, now=now, after_days=after_days, repeat_days=repeat_days
    )
    if not grouped:
        logger.info("nothing to remind about")
        return {"bills": 0, "reminded": 0}

    receivers = {r.phone: r for r in load_receivers(table)}
    reminded = 0

    for pk, rows in sorted(grouped.items()):
        bill = table.get_item(Key={"PK": pk, "SK": "META"}).get("Item") or {}
        messages: dict[str, str] = {}

        for row in rows:
            phone = row["phone"] if "phone" in row else row["SK"][len(PAY_PREFIX):]
            receiver = receivers.get(phone)
            if receiver is None:
                # Removed from config.yaml since the bill went out.
                continue
            values = bill_values(
                biller=bill.get("provider_name", "the"),
                total=bill.get("total", Decimal("0")),
                amount=row.get("amount_owed", Decimal("0")),
                label=receiver.label,
                due_date=bill.get("due_date") or config.no_due_date_text,
                month=bill.get("month", ""),
                venmo_username=config.venmo_username,
                zelle_contact=config.zelle_contact,
                venmo_note=config.template("venmo_note"),
            )
            messages[phone] = render(config.template("reminder"), values)

        if not messages:
            continue

        logger.info("reminding %d roommate(s) about %s", len(messages), pk)
        send_all(
            messages,
            dry_run=config.dry_run,
            origination_number_id=config.origination_number_id,
            client=sms_client,
        )
        for phone in messages:
            _mark_reminded(table, pk, phone, now)
        reminded += len(messages)

    return {"bills": len(grouped), "reminded": reminded}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    config = RuntimeConfig.from_env()
    table = boto3.resource("dynamodb").Table(config.table_name)
    return send_reminders(
        table,
        config,
        after_days=int(os.environ.get("REMINDER_AFTER_DAYS", "3")),
        repeat_days=int(os.environ.get("REMINDER_REPEAT_DAYS", "3")),
    )
