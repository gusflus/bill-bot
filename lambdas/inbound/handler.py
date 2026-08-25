"""Inbound Lambda: handles roommates texting back.

AWS End User Messaging publishes inbound SMS to an SNS topic; this is subscribed
to it. Two keywords are understood:

    PAID    mark yourself paid on your most recent unpaid bill
    STATUS  reply with who has paid

Identity comes from the originating phone number, matched against the configured
receivers. That works cleanly *because* bills are sent 1:1 rather than to a group
thread - there's no ambiguity about who sent a reply, and no name to parse out of
the message text.

STOP, HELP, and UNSTOP are reserved by carriers and handled upstream by AWS, so
they're ignored here rather than shadowed.

Worth stating plainly: PAID is self-reported. There is no payment API to verify
against for personal Venmo or Zelle, so anyone texting from a roommate's number
can mark that roommate paid. Fine for a household, not a payment system.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

from render import render, tally_values
from runtime_config import RuntimeConfig
from sender import send_all
from store import load_receivers, mark_paid, tally

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

PAID = "PAID"
STATUS = "STATUS"
# Carrier-reserved: AWS handles opt-out itself, so we must not shadow these.
RESERVED = {"STOP", "UNSTOP", "START", "HELP", "INFO", "CANCEL", "END", "QUIT",
            "UNSUBSCRIBE", "SUBSCRIBE", "ARRET"}


def parse_keyword(body: str) -> str | None:
    """Normalize an inbound message to a keyword we act on, or None.

    Tolerates casing, surrounding whitespace, and trailing punctuation, because
    people text "paid!" and "Paid." as readily as "PAID".
    """
    cleaned = (body or "").strip().strip(".,!?;:").upper()
    if not cleaned:
        return None
    # Accept a keyword sent as the whole message, or as its first word
    # ("paid thanks").
    first = cleaned.split()[0] if cleaned.split() else ""
    for candidate in (cleaned, first):
        if candidate in (PAID, STATUS):
            return candidate
    return None


def _bill_meta(table, bill_pk: str) -> dict[str, Any]:
    item = table.get_item(Key={"PK": bill_pk, "SK": "META"}).get("Item") or {}
    return item


def handle_message(
    table, *, phone: str, body: str, config: RuntimeConfig, sms_client=None
) -> dict[str, Any]:
    """Act on one inbound text. Returns a summary for logging and tests."""
    keyword = parse_keyword(body)
    if keyword is None:
        stripped = (body or "").strip().upper()
        if stripped in RESERVED:
            # AWS already handled the opt-out; don't reply and don't log a number.
            logger.info("ignoring carrier-reserved keyword")
            return {"action": "ignored", "reason": "reserved keyword"}
        logger.info("unrecognized message, ignoring")
        return {"action": "ignored", "reason": "unrecognized keyword"}

    receivers = load_receivers(table)
    receiver = next((r for r in receivers if r.phone == phone), None)
    if receiver is None:
        # Someone not in config.yaml. Silence is the right answer - replying
        # would confirm the number is live to whoever is probing it.
        logger.warning("message from a number that is not a configured receiver")
        return {"action": "ignored", "reason": "unknown sender"}

    from store import find_bills_for

    # Prefer an unpaid bill. Falling back to the most recent bill they're on at
    # all means a second PAID still gets acknowledged - texting PAID and hearing
    # nothing back looks like the bot is broken.
    open_bills = find_bills_for(table, phone, unpaid_only=True)
    bills = open_bills or find_bills_for(table, phone, unpaid_only=False)
    if not bills:
        logger.info("%s is not on any bill", receiver.label)
        return {"action": "ignored", "reason": "no open bills"}

    bill_pk = bills[0]["PK"]
    meta = _bill_meta(table, bill_pk)
    biller = meta.get("provider_name", "the")
    month = meta.get("month", "")

    if keyword == PAID:
        flipped = mark_paid(table, bill_pk, phone)
        paid_count, receiver_count = tally(table, bill_pk)
        amount_owed = bills[0].get("amount_owed", 0)
        logger.info(
            "%s marked paid on %s (%s); tally now %d/%d",
            receiver.label,
            bill_pk,
            "changed" if flipped else "already paid",
            paid_count,
            receiver_count,
        )
        text = render(
            config.template("paid_ack"),
            tally_values(
                biller=biller,
                month=month,
                paid_count=paid_count,
                receiver_count=receiver_count,
                label=receiver.label,
                amount=amount_owed,
            ),
        )
        outbound = {phone: text}

        # Tell the payer, but only on a real state change - re-acknowledging a
        # roommate who texts PAID twice shouldn't buzz the payer twice.
        if config.alert_payer and flipped:
            outbound[config.payer_phone] = render(
                config.template("payment_alert"),
                tally_values(
                    biller=biller,
                    month=month,
                    paid_count=paid_count,
                    receiver_count=receiver_count,
                    label=receiver.label,
                    amount=amount_owed,
                    total=meta.get("total", 0),
                ),
            )

        send_all(
            outbound,
            dry_run=config.dry_run,
            origination_number_id=config.origination_number_id,
            client=sms_client,
        )
        return {
            "action": "paid",
            "bill": bill_pk,
            "changed": flipped,
            "paid": paid_count,
            "of": receiver_count,
            "payerAlerted": config.alert_payer and flipped,
        }

    paid_count, receiver_count = tally(table, bill_pk)
    text = render(
        config.template("status"),
        tally_values(
            biller=biller,
            month=month,
            paid_count=paid_count,
            receiver_count=receiver_count,
            total=meta.get("total", 0),
        ),
    )
    send_all(
        {phone: text},
        dry_run=config.dry_run,
        origination_number_id=config.origination_number_id,
        client=sms_client,
    )
    return {
        "action": "status",
        "bill": bill_pk,
        "paid": paid_count,
        "of": receiver_count,
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    config = RuntimeConfig.from_env()
    table = boto3.resource("dynamodb").Table(config.table_name)

    results = []
    for record in event.get("Records", []):
        try:
            message = json.loads(record["Sns"]["Message"])
        except (KeyError, json.JSONDecodeError):
            logger.exception("could not parse SNS record")
            continue

        phone = message.get("originationNumber")
        body = message.get("messageBody", "")
        if not phone:
            logger.warning("inbound message had no originationNumber")
            continue

        try:
            results.append(handle_message(table, phone=phone, body=body, config=config))
        except Exception:
            # Don't raise: one bad message must not make SNS redeliver the batch
            # and re-send acknowledgements to everyone else.
            logger.exception("failed handling an inbound message")
            results.append({"action": "error"})

    return {"handled": len(results), "results": results}
