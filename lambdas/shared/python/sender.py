"""Sending SMS through AWS End User Messaging.

One message per receiver, each pinned to the specific origination number rather
than a phone pool. Pools pick an identity per message based on destination and
sticky-sending history, which is fine for marketing blasts but means the number
your roommates see could change. Pinning makes the sender number deterministic,
so replies land in a thread they recognize.

A failure for one recipient never aborts the rest. Five roommates getting their
text is a much better outcome than nobody getting one because the sixth number
was mistyped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Delivery:
    """Outcome of a fan-out."""

    sent: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    logged: list[str] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return bool(self.logged) and not self.sent


def send_one(client, *, phone: str, text: str, origination_number_id: str) -> None:
    client.send_text_message(
        DestinationPhoneNumber=phone,
        OriginationIdentity=origination_number_id,
        MessageBody=text,
        MessageType="TRANSACTIONAL",
    )


def send_all(
    messages: dict[str, str],
    *,
    dry_run: bool,
    origination_number_id: str,
    client=None,
) -> Delivery:
    """Send ``{phone: text}``, or log it when dry_run is on."""
    delivery = Delivery()

    if dry_run:
        for phone, text in messages.items():
            # Indented so a multi-line message stays readable in CloudWatch.
            indented = "\n    ".join(text.splitlines())
            logger.info("[dry_run] would text %s:\n    %s", phone, indented)
            delivery.logged.append(phone)
        logger.info(
            "[dry_run] %d message(s) logged, none sent. Set behavior.dry_run: "
            "false in config.yaml to send for real.",
            len(delivery.logged),
        )
        return delivery

    if not origination_number_id:
        raise ValueError(
            "origination_number_id is required to send; either set "
            "messaging.origination_number_id or keep behavior.dry_run: true"
        )

    if client is None:
        import boto3

        client = boto3.client("pinpoint-sms-voice-v2")

    for phone, text in messages.items():
        try:
            send_one(
                client,
                phone=phone,
                text=text,
                origination_number_id=origination_number_id,
            )
            delivery.sent.append(phone)
        except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
            logger.exception("failed to text %s", phone)
            delivery.failed.append({"phone": phone, "error": str(exc)})

    logger.info("sent %d, failed %d", len(delivery.sent), len(delivery.failed))
    return delivery
