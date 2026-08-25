"""Ingest Lambda: turns a forwarded bill email into texts.

Reached over a Lambda Function URL by the Apps Script watcher.

    GET  /senders  -> the sender list, so Apps Script knows what to search for
    POST /bills    -> one email to process

The status code is a contract with Apps Script, which uses it to decide how to
label the Gmail thread:

    2xx  handled (notified, ignored, or a duplicate) -> label Processed
    4xx  will never succeed (bad sender, malformed)  -> label Error
    5xx  might succeed later (Bedrock down, throttle) -> leave unlabeled, retry

That's why an unparseable bill returns 200 with ``action: ignored`` rather than
an error: it's a final answer, not a transient failure, and retrying it forever
would be pointless.

Security note: this URL is publicly reachable with auth type NONE. The shared
secret header is the only thing standing in front of it, so the comparison uses
hmac.compare_digest, the body is size-limited, and reserved concurrency is
capped to bound what an attacker who finds the URL can spend on Bedrock.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3

from extract import billing_month, extract
from render import bill_values, render
from runtime_config import RuntimeConfig
from sender import send_all
from shares import allocate, format_money
from store import (
    STATUS_HELD,
    STATUS_NOTIFIED,
    Decision,
    find_sender,
    load_receivers,
    load_senders,
    record_bill,
    set_bill_status,
    write_ledger,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SECRET_HEADER = "x-bill-bot-secret"
# Generous for an email body, small enough that nobody can make us pay to parse
# a megabyte of junk.
MAX_BODY_BYTES = 512 * 1024

_secret_cache: str | None = None


# --- auth ---------------------------------------------------------------------


def _shared_secret(secret_arn: str) -> str:
    """Fetch and cache the shared secret for this container's lifetime."""
    global _secret_cache
    if _secret_cache is None:
        client = boto3.client("secretsmanager")
        _secret_cache = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    return _secret_cache


def _authorized(event: dict[str, Any], secret_arn: str) -> bool:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    presented = headers.get(SECRET_HEADER, "")
    if not presented:
        return False
    # compare_digest, not ==, so response timing doesn't leak the secret.
    return hmac.compare_digest(presented, _shared_secret(secret_arn))


# --- HTTP plumbing ------------------------------------------------------------


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _route(event: dict[str, Any]) -> tuple[str, str]:
    ctx = event.get("requestContext", {}).get("http", {})
    return ctx.get("method", "GET").upper(), event.get("rawPath", "/")


# --- handlers -----------------------------------------------------------------


def handle_get_senders(table) -> dict[str, Any]:
    senders = load_senders(table)
    return _response(
        200,
        {
            "senders": [
                {"id": s.id, "name": s.name, "fromAddress": s.from_address}
                for s in senders
            ]
        },
    )


def handle_post_bill(
    table, payload: dict[str, Any], config: RuntimeConfig, sms_client=None
) -> dict[str, Any]:
    message_id = payload.get("messageId")
    from_header = payload.get("from", "")
    body_text = payload.get("bodyText", "")

    if not message_id:
        return _response(400, {"error": "messageId is required"})

    senders = load_senders(table)
    sender = find_sender(senders, from_header)
    if sender is None:
        # A permanent condition: Apps Script shouldn't retry this forever.
        return _response(
            400,
            {
                "error": "no configured sender matches this From header",
                "from": from_header,
            },
        )

    received_at = _parse_received_at(payload.get("receivedAt"))
    month = billing_month(received_at, config.timezone)

    result = extract(
        body_text,
        model_id=config.model_id,
        subject=payload.get("subject", ""),
        sender=sender.name,
    )
    logger.info(
        "extracted provider=%s month=%s amount=%s due=%s class=%s conf=%s src=%s",
        sender.id,
        month,
        result.amount,
        result.due_date,
        result.classification,
        result.confidence,
        result.source,
    )

    if not result.is_billable:
        # Not a bill, or no amount to split. Final answer, so 200.
        return _response(
            200,
            {
                "action": "ignored",
                "reason": f"not billable (classification={result.classification})",
                "classification": result.classification,
                "notes": result.notes,
            },
        )

    hold = config.on_low_confidence == "hold" and result.confidence == "low"

    outcome = record_bill(
        table,
        provider_id=sender.id,
        provider_name=sender.name,
        month=month,
        total=result.amount,
        due_date=result.due_date,
        classification=result.classification,
        confidence=result.confidence,
        status=STATUS_HELD if hold else STATUS_NOTIFIED,
        thread_id=payload.get("threadId", ""),
        message_id=message_id,
        record_ttl_days=config.record_ttl_days,
    )

    if not outcome.should_notify:
        return _response(
            200,
            {
                "action": outcome.decision.value,
                "bill": outcome.bill_pk,
                "amount": result.amount,
            },
        )

    receivers = load_receivers(table)
    if not receivers:
        # Config seeding must have failed - worth retrying, so 5xx.
        return _response(500, {"error": "no active receivers configured"})

    amounts, payer_amount = split_bill(
        result.amount, receivers, config.payer_share
    )
    write_ledger(
        table,
        pk=outcome.bill_pk,
        amounts=amounts,
        receivers=receivers,
        record_ttl_days=config.record_ttl_days,
        payer_amount=payer_amount,
    )

    if hold:
        set_bill_status(table, outcome.bill_pk, STATUS_HELD)
        logger.warning(
            "holding %s: confidence=low and on_low_confidence=hold. "
            "Amount %s, notes: %s",
            outcome.bill_pk,
            result.amount,
            result.notes,
        )
        return _response(
            200,
            {
                "action": "held",
                "bill": outcome.bill_pk,
                "amount": result.amount,
                "reason": result.notes or "low confidence",
            },
        )

    messages = build_messages(
        config=config,
        sender_name=sender.name,
        total=result.amount,
        amounts=amounts,
        receivers=receivers,
        month=month,
        due_date=result.due_date,
        corrected_from=outcome.previous_amount,
    )
    delivery = send_all(
        messages,
        dry_run=config.dry_run,
        origination_number_id=config.origination_number_id,
        client=sms_client,
    )

    return _response(
        200,
        {
            "action": outcome.decision.value,
            "bill": outcome.bill_pk,
            "amount": result.amount,
            "confidence": result.confidence,
            "payerShare": payer_amount,
            "dryRun": config.dry_run,
            "sent": delivery.sent,
            "failed": delivery.failed,
        },
    )


def split_bill(
    total: Decimal, receivers: list, payer_share: float
) -> tuple[dict[str, Decimal], Decimal]:
    """Divide ``total`` across the receivers and the payer.

    Returns ``({phone: amount}, payer_amount)``. The payer's weight is in the
    denominator so nobody is overcharged to cover a portion the payer is already
    absorbing, but they get no ledger row - there is nothing to collect from the
    person who paid the utility.

    Cents are allocated across everyone at once, so
    ``sum(amounts) + payer_amount`` equals the total exactly.
    """
    weights = [r.weight for r in receivers]
    if payer_share > 0:
        parts = allocate(total, weights + [payer_share])
        return dict(zip((r.phone for r in receivers), parts)), parts[-1]

    parts = allocate(total, weights)
    return dict(zip((r.phone for r in receivers), parts)), Decimal("0.00")


def build_messages(
    *,
    config: RuntimeConfig,
    sender_name: str,
    total: Decimal,
    amounts: dict[str, Decimal],
    receivers: list,
    month: str,
    due_date,
    corrected_from: Decimal | None = None,
) -> dict[str, str]:
    """Render one message per receiver. Returns {phone: text}.

    The payer is not in ``receivers``, so they never appear here.
    """
    template = config.template("bill")

    prefix = ""
    if corrected_from is not None:
        # Say so plainly - a second text with a different number is confusing
        # otherwise.
        prefix = f"Correction (was {format_money(corrected_from)}).\n"

    out: dict[str, str] = {}
    for receiver in receivers:
        values = bill_values(
            biller=sender_name,
            total=total,
            amount=amounts[receiver.phone],
            label=receiver.label,
            due_date=(
                due_date.strftime("%b %-d")
                if due_date
                else config.no_due_date_text
            ),
            month=month,
            venmo_username=config.venmo_username,
            zelle_contact=config.zelle_contact,
            venmo_note=config.template("venmo_note"),
        )
        out[receiver.phone] = prefix + render(template, values)
    return out


def _parse_received_at(value: Any) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("could not parse receivedAt %r; using now", value)
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- entry point --------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    config = RuntimeConfig.from_env()

    if not _authorized(event, config.secret_arn):
        logger.warning("rejected unauthorized request")
        return _response(401, {"error": "unauthorized"})

    method, path = _route(event)
    table = boto3.resource("dynamodb").Table(config.table_name)

    if method == "GET" and path.rstrip("/").endswith("/senders"):
        return handle_get_senders(table)

    if method != "POST":
        return _response(405, {"error": f"{method} {path} not supported"})

    raw = event.get("body") or ""
    if len(raw.encode("utf-8", errors="ignore")) > MAX_BODY_BYTES:
        return _response(413, {"error": "body too large"})

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _response(400, {"error": f"invalid JSON body: {exc}"})
    if not isinstance(payload, dict):
        return _response(400, {"error": "body must be a JSON object"})

    try:
        return handle_post_bill(table, payload, config)
    except Exception:
        # 5xx so Apps Script leaves the thread unlabeled and retries. Losing a
        # bill is worse than processing it late.
        logger.exception("unhandled error processing bill")
        return _response(500, {"error": "internal error"})
