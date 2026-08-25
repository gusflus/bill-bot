"""Runtime settings, read from Lambda environment variables.

Split by how often a value changes and who needs to see it:

- Receivers and senders live in DynamoDB, because they're the list you edit and
  they're read per invocation.
- Everything here is a deploy-time constant, so an env var is simpler and
  cheaper than a table read on every cold start.

Both are populated from the same ``config.yaml`` by ``cdk deploy``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeConfig:
    table_name: str
    model_id: str
    timezone: str
    dry_run: bool
    on_low_confidence: str
    no_due_date_text: str
    record_ttl_days: int
    venmo_username: str
    zelle_contact: str | None
    origination_number_id: str
    secret_arn: str
    payer_label: str = "Me"
    payer_phone: str | None = None
    payer_share: float = 0.0
    notify_on_payment: bool = False
    messages: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RuntimeConfig:
        e = env if env is not None else os.environ
        return cls(
            table_name=e["TABLE_NAME"],
            model_id=e.get("BEDROCK_MODEL_ID", ""),
            timezone=e.get("TIMEZONE", "UTC"),
            # Anything other than an explicit "false" keeps dry-run on, so a
            # missing or misspelled variable can't start texting people.
            dry_run=e.get("DRY_RUN", "true").lower() != "false",
            on_low_confidence=e.get("ON_LOW_CONFIDENCE", "send"),
            no_due_date_text=e.get("NO_DUE_DATE_TEXT", "date unknown"),
            record_ttl_days=int(e.get("RECORD_TTL_DAYS", "400")),
            venmo_username=e.get("VENMO_USERNAME", ""),
            zelle_contact=e.get("ZELLE_CONTACT") or None,
            origination_number_id=e.get("ORIGINATION_NUMBER_ID", ""),
            secret_arn=e.get("SECRET_ARN", ""),
            payer_label=e.get("PAYER_LABEL", "Me"),
            payer_phone=e.get("PAYER_PHONE") or None,
            payer_share=float(e.get("PAYER_SHARE", "0") or 0),
            notify_on_payment=e.get("NOTIFY_ON_PAYMENT", "false").lower() == "true",
            messages=json.loads(e.get("MESSAGES", "{}")),
        )

    @property
    def alert_payer(self) -> bool:
        """Whether to text the payer when someone settles up."""
        return self.notify_on_payment and bool(self.payer_phone)

    def template(self, name: str) -> str:
        try:
            return self.messages[name]
        except KeyError:
            raise KeyError(
                f"message template {name!r} missing from MESSAGES; "
                f"have {sorted(self.messages)}"
            ) from None
