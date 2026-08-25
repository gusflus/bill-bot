"""Deployment configuration loader.

Reads ``config.yaml`` from the repo root. This is one-household-per-clone, so
config.yaml is gitignored and holds your roommates' phone numbers.

Everything the bot does is driven from here: who pays, how much of each bill
they owe, what the texts say, and which senders count as bills. The Lambdas
never read this file - ``cdk deploy`` seeds it into DynamoDB and they read
that. See ``infra/constructs/state_table.py``.

Validation is deliberately strict and happens at synth time, so a typo fails
``cdk deploy`` instead of surfacing at 3am when a bill arrives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

_REPO_ROOT = Path(__file__).parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.yaml"

DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_STACK_SUFFIX_RE = re.compile(r"[a-z0-9-]{1,32}")
# E.164: leading +, first digit non-zero, 8-15 digits total.
_E164_RE = re.compile(r"\+[1-9]\d{7,14}")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Which placeholders each message type can actually resolve. A message rendered
# without a specific receiver in hand (status) can't use {amount} or {label};
# a message rendered without a tally (bill) can't use {paid_count}. Enforcing
# this at deploy time is the whole point.
_PER_RECEIVER = frozenset(
    {
        "biller",
        "total",
        "amount",
        "label",
        "due_date",
        "month",
        "venmo_link",
        "zelle_line",
    }
)
ALLOWED_PLACEHOLDERS: Mapping[str, frozenset[str]] = {
    "bill": _PER_RECEIVER,
    "paid_ack": frozenset(
        {"biller", "label", "amount", "month", "paid_count", "receiver_count"}
    ),
    "status": frozenset({"biller", "month", "total", "paid_count", "receiver_count"}),
    "reminder": _PER_RECEIVER,
    # Sent to the payer when someone settles up. {label} and {amount} are the
    # person who paid and what they owed, not the payer.
    "payment_alert": frozenset(
        {"biller", "label", "amount", "month", "total", "paid_count", "receiver_count"}
    ),
    # The comment Venmo pre-fills, which ends up in both parties' payment
    # history. No {venmo_link} or {zelle_line} here - it goes *inside* the link.
    "venmo_note": frozenset({"biller", "label", "amount", "total", "month", "due_date"}),
}

# Representative values used only to estimate SMS segment counts at synth time.
# Lengths matter, exact contents don't.
_SAMPLE_VALUES = {
    "biller": "SoCalGas",
    "total": "$142.53",
    "amount": "$23.76",
    "label": "Alex",
    "due_date": "Sep 12",
    "month": "2026-08",
    "venmo_link": (
        "https://venmo.com/?txn=pay&audience=private&recipients=your-venmo-handle"
        "&amount=23.76&note=SoCalGas%20bill%20split"
    ),
    "zelle_line": "\nOr Zelle $23.76 to you@example.com",
    "paid_count": "4",
    "receiver_count": "6",
}

# GSM-7 default alphabet plus its extension table. Anything outside this forces
# the whole message to UCS-2, which cuts the per-segment budget from 160 to 70.
_GSM7_CHARS = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENDED = set("^{}\\[~]|€")


class ConfigError(ValueError):
    """Raised for any problem in config.yaml, always naming the field."""


def slugify(value: str) -> str:
    """'PG&E' -> 'pg-e'. Used to derive a stable sender id for DynamoDB keys."""
    return _SLUG_STRIP_RE.sub("-", value.lower()).strip("-")


def _reject_unknown_keys(cls: type, data: Mapping[str, Any], where: str) -> None:
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {sorted(unknown)}. "
            f"Allowed here: {sorted(allowed)}"
        )


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return dict(value)


def _require_list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list, got {type(value).__name__}")
    return value


def segment_count(text: str) -> int:
    """Number of SMS segments ``text`` would occupy.

    GSM-7 fits 160 chars in one segment, 153 per segment once concatenated.
    A single non-GSM-7 character (an emoji, a curly quote, an em dash) forces
    UCS-2 for the whole message, dropping that to 70 and 67. Callers use this
    to warn about templates that quietly cost 3x per send.
    """
    if not text:
        return 0

    if all(c in _GSM7_CHARS or c in _GSM7_EXTENDED for c in text):
        # Extension-table characters are transmitted as two septets.
        length = sum(2 if c in _GSM7_EXTENDED else 1 for c in text)
        single, multi = 160, 153
    else:
        # Characters outside the BMP take two UTF-16 code units.
        length = sum(2 if ord(c) > 0xFFFF else 1 for c in text)
        single, multi = 70, 67

    if length <= single:
        return 1
    return -(-length // multi)  # ceiling division


def _validate_template(name: str, template: str) -> None:
    allowed = ALLOWED_PLACEHOLDERS[name]
    used = set(_PLACEHOLDER_RE.findall(template))
    unknown = used - allowed
    if unknown:
        raise ConfigError(
            f"messages.{name}: unknown placeholder(s) "
            f"{sorted('{' + u + '}' for u in unknown)}. "
            f"Available here: {sorted('{' + a + '}' for a in sorted(allowed))}"
        )


def render_sample(name: str, template: str) -> str:
    """Fill a template with representative values, for segment estimation."""
    return template.format(
        **{k: v for k, v in _SAMPLE_VALUES.items() if k in ALLOWED_PLACEHOLDERS[name]}
    )


@dataclass(frozen=True)
class Payee:
    """Who everyone pays, and their own stake in each bill.

    The payer is not a receiver: they front the money to the utility, so texting
    them a request to pay themselves would be nonsense. They still carry a
    ``share``, because their portion has to be in the denominator for everyone
    else's share to be right — it's simply absorbed rather than collected.

    Set ``share: 0`` if the payer isn't part of the split at all and the
    receivers cover the whole bill.
    """

    venmo_username: str = ""
    zelle_contact: str | None = None
    label: str = "Me"
    phone: str | None = None
    share: float = 1.0
    notify_on_payment: bool = False

    @classmethod
    def parse(cls, data: Any) -> Payee:
        data = _require_mapping(data, "payee")
        _reject_unknown_keys(cls, data, "payee")
        payee = cls(**data)

        if not payee.venmo_username.strip():
            raise ConfigError("payee.venmo_username: required and cannot be empty")
        if payee.venmo_username.startswith("@"):
            raise ConfigError(
                "payee.venmo_username: drop the leading '@' "
                f"(got {payee.venmo_username!r})"
            )
        if not payee.label.strip():
            raise ConfigError("payee.label: cannot be empty")
        if payee.phone is not None and not _E164_RE.fullmatch(str(payee.phone)):
            raise ConfigError(
                f"payee.phone: {payee.phone!r} is not E.164 format "
                "(needs a leading '+' and country code, e.g. '+15551230001')"
            )
        if not isinstance(payee.share, (int, float)) or isinstance(payee.share, bool):
            raise ConfigError("payee.share: must be a number")
        if payee.share < 0:
            raise ConfigError(
                f"payee.share: cannot be negative (got {payee.share}). "
                "Use 0 if the payer isn't part of the split."
            )
        if not isinstance(payee.notify_on_payment, bool):
            raise ConfigError("payee.notify_on_payment: must be true or false")
        if payee.notify_on_payment and not payee.phone:
            raise ConfigError(
                "payee.notify_on_payment: needs payee.phone set, or there is "
                "nowhere to send the alert"
            )
        return payee

    @property
    def in_split(self) -> bool:
        return self.share > 0


@dataclass(frozen=True)
class Receiver:
    """One roommate and their relative share of every bill."""

    label: str
    phone: str
    share: float = 1.0

    @classmethod
    def parse(cls, data: Any, index: int) -> Receiver:
        where = f"receivers[{index}]"
        data = _require_mapping(data, where)
        _reject_unknown_keys(cls, data, where)
        missing = {"label", "phone"} - set(data)
        if missing:
            raise ConfigError(f"{where}: missing required key(s) {sorted(missing)}")

        receiver = cls(**data)
        if not receiver.label.strip():
            raise ConfigError(f"{where}.label: cannot be empty")
        if not _E164_RE.fullmatch(str(receiver.phone)):
            raise ConfigError(
                f"{where}.phone: {receiver.phone!r} is not E.164 format "
                "(needs a leading '+' and country code, e.g. '+15551230001')"
            )
        if not isinstance(receiver.share, (int, float)) or isinstance(
            receiver.share, bool
        ):
            raise ConfigError(f"{where}.share: must be a number")
        if receiver.share <= 0:
            raise ConfigError(
                f"{where}.share: must be greater than 0 (got {receiver.share})"
            )
        return receiver


@dataclass(frozen=True)
class Sender:
    """A bill sender to watch. ``id`` is derived from ``name`` unless given."""

    name: str
    from_address: str
    id: str = ""

    @classmethod
    def parse(cls, data: Any, index: int) -> Sender:
        where = f"senders[{index}]"
        data = _require_mapping(data, where)
        _reject_unknown_keys(cls, data, where)
        missing = {"name", "from_address"} - set(data)
        if missing:
            raise ConfigError(f"{where}: missing required key(s) {sorted(missing)}")

        resolved = dict(data)
        if not resolved.get("id"):
            resolved["id"] = slugify(resolved["name"])

        sender = cls(**resolved)
        if not sender.name.strip():
            raise ConfigError(f"{where}.name: cannot be empty")
        if not sender.from_address.strip():
            raise ConfigError(f"{where}.from_address: cannot be empty")
        if not sender.id:
            raise ConfigError(
                f"{where}.id: could not derive an id from name {sender.name!r}; "
                "set 'id' explicitly"
            )
        return sender


@dataclass(frozen=True)
class Messages:
    """Every outbound string. Placeholders validated per message type."""

    bill: str = (
        "{biller} bill received: {total} total.\n"
        "Your share, {label}: {amount} - due {due_date}\n"
        "Pay: {venmo_link}{zelle_line}"
    )
    paid_ack: str = (
        "Thanks {label}! {paid_count} of {receiver_count} paid the {biller} bill."
    )
    status: str = "{biller} {month}: {paid_count} of {receiver_count} paid."
    reminder: str = "Reminder: {amount} still owed for the {biller} bill. {venmo_link}"
    payment_alert: str = (
        "{label} paid {amount} for the {biller} bill. "
        "{paid_count} of {receiver_count} in."
    )
    # Pre-filled as the Venmo payment comment. Venmo caps notes at 280
    # characters and this never gets near that, so it isn't length-validated.
    venmo_note: str = "{biller} bill split"

    @classmethod
    def parse(cls, data: Any) -> Messages:
        data = _require_mapping(data, "messages")
        _reject_unknown_keys(cls, data, "messages")
        for key, value in data.items():
            if not isinstance(value, str):
                raise ConfigError(
                    f"messages.{key}: expected a string, got {type(value).__name__}"
                )
        messages = cls(**data)
        for f in fields(cls):
            _validate_template(f.name, getattr(messages, f.name))
        if not messages.bill.strip():
            raise ConfigError("messages.bill: cannot be empty")
        if not messages.venmo_note.strip():
            raise ConfigError(
                "messages.venmo_note: cannot be empty - Venmo payments need a note"
            )
        return messages

    def segment_estimates(self) -> dict[str, int]:
        """Segment count per message type, using representative sample values.

        ``venmo_note`` is excluded: it's never sent as an SMS on its own, it
        rides inside the Venmo URL in whichever message carries the link.
        """
        return {
            f.name: segment_count(render_sample(f.name, getattr(self, f.name)))
            for f in fields(self)
            if f.name != "venmo_note"
        }


@dataclass(frozen=True)
class Behavior:
    """Runtime toggles that don't belong to any single resource."""

    dry_run: bool = True
    lookback_days: int = 14
    processed_label: str = "Bill-Bot/Processed"
    error_label: str = "Bill-Bot/Error"
    on_low_confidence: str = "send"
    no_due_date_text: str = "date unknown"
    record_ttl_days: int = 400

    @classmethod
    def parse(cls, data: Any) -> Behavior:
        data = _require_mapping(data, "behavior")
        _reject_unknown_keys(cls, data, "behavior")
        behavior = cls(**data)

        if not isinstance(behavior.dry_run, bool):
            raise ConfigError("behavior.dry_run: must be true or false")
        if behavior.on_low_confidence not in ("send", "hold"):
            raise ConfigError(
                "behavior.on_low_confidence: must be 'send' or 'hold' "
                f"(got {behavior.on_low_confidence!r})"
            )
        for name in ("lookback_days", "record_ttl_days"):
            value = getattr(behavior, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError(f"behavior.{name}: must be an integer >= 1")
        for name in ("processed_label", "error_label"):
            if not getattr(behavior, name).strip():
                raise ConfigError(f"behavior.{name}: cannot be empty")
        if behavior.processed_label == behavior.error_label:
            raise ConfigError(
                "behavior.processed_label and behavior.error_label must differ"
            )
        return behavior


@dataclass(frozen=True)
class Reminders:
    """Nudges for roommates who haven't paid.

    Off by default: an automated bot texting people about money is the kind of
    thing that should be opted into deliberately.
    """

    enabled: bool = False
    after_days: int = 3
    repeat_days: int = 3
    hour_utc: int = 17

    @classmethod
    def parse(cls, data: Any) -> Reminders:
        data = _require_mapping(data, "reminders")
        _reject_unknown_keys(cls, data, "reminders")
        reminders = cls(**data)

        if not isinstance(reminders.enabled, bool):
            raise ConfigError("reminders.enabled: must be true or false")
        for name in ("after_days", "repeat_days"):
            value = getattr(reminders, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError(f"reminders.{name}: must be an integer >= 1")
        if (
            not isinstance(reminders.hour_utc, int)
            or isinstance(reminders.hour_utc, bool)
            or not 0 <= reminders.hour_utc <= 23
        ):
            raise ConfigError("reminders.hour_utc: must be an integer 0-23")
        return reminders


@dataclass(frozen=True)
class Messaging:
    """AWS End User Messaging settings.

    SMS only, on purpose: MMS costs 3x for media we never send, and RCS costs
    $500 up front plus $200/mo in agent maintenance. See TODO.md.
    """

    channel: str = "sms"
    origination_number_id: str = ""
    max_segments_warn: int = 2

    @classmethod
    def parse(cls, data: Any) -> Messaging:
        data = _require_mapping(data, "messaging")
        _reject_unknown_keys(cls, data, "messaging")
        messaging = cls(**data)

        if messaging.channel != "sms":
            raise ConfigError(
                f"messaging.channel: only 'sms' is supported (got "
                f"{messaging.channel!r}). MMS and RCS are deliberately "
                "unsupported - see TODO.md for the cost rationale."
            )
        if (
            not isinstance(messaging.max_segments_warn, int)
            or isinstance(messaging.max_segments_warn, bool)
            or messaging.max_segments_warn < 1
        ):
            raise ConfigError("messaging.max_segments_warn: must be an integer >= 1")
        return messaging


@dataclass(frozen=True)
class DeployConfig:
    """Resolved configuration for the deployment."""

    stack_suffix: str = "home"
    region: str | None = None
    timezone: str = "America/Los_Angeles"
    bedrock_model_id: str = DEFAULT_BEDROCK_MODEL
    payee: Payee = field(default_factory=Payee)
    receivers: tuple[Receiver, ...] = ()
    senders: tuple[Sender, ...] = ()
    messages: Messages = field(default_factory=Messages)
    behavior: Behavior = field(default_factory=Behavior)
    messaging: Messaging = field(default_factory=Messaging)
    reminders: Reminders = field(default_factory=Reminders)

    @property
    def stack_name(self) -> str:
        return f"bill-bot-{self.stack_suffix}-stack"

    def total_share_weight(self) -> float:
        """Every weight the bill divides by, the payer's included.

        The payer's share has to be in here even though they're never billed, or
        everyone else's share would be inflated to cover a portion nobody owes.
        """
        return sum(r.share for r in self.receivers) + self.payee.share


def _parse(loaded: Mapping[str, Any]) -> DeployConfig:
    _reject_unknown_keys(DeployConfig, loaded, "config.yaml")

    receivers = tuple(
        Receiver.parse(item, i)
        for i, item in enumerate(_require_list(loaded.get("receivers"), "receivers"))
    )
    senders = tuple(
        Sender.parse(item, i)
        for i, item in enumerate(_require_list(loaded.get("senders"), "senders"))
    )

    config = DeployConfig(
        stack_suffix=loaded.get("stack_suffix", "home"),
        region=loaded.get("region"),
        timezone=loaded.get("timezone", "America/Los_Angeles"),
        bedrock_model_id=loaded.get("bedrock_model_id", DEFAULT_BEDROCK_MODEL),
        payee=Payee.parse(loaded.get("payee")),
        receivers=receivers,
        senders=senders,
        messages=Messages.parse(loaded.get("messages")),
        behavior=Behavior.parse(loaded.get("behavior")),
        messaging=Messaging.parse(loaded.get("messaging")),
        reminders=Reminders.parse(loaded.get("reminders")),
    )

    if not _STACK_SUFFIX_RE.fullmatch(config.stack_suffix):
        raise ConfigError(
            f"stack_suffix: {config.stack_suffix!r} must match "
            f"{_STACK_SUFFIX_RE.pattern}"
        )
    try:
        ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"timezone: {config.timezone!r} is not a known IANA zone ({exc})"
        ) from exc
    if not config.bedrock_model_id.strip():
        raise ConfigError("bedrock_model_id: cannot be empty")

    if not config.receivers:
        raise ConfigError("receivers: at least one receiver is required")
    if not config.senders:
        raise ConfigError("senders: at least one sender is required")

    _reject_duplicates(
        [r.phone for r in config.receivers], "receivers", "phone"
    )
    _reject_duplicates(
        [s.from_address.lower() for s in config.senders], "senders", "from_address"
    )
    _reject_duplicates([s.id for s in config.senders], "senders", "id")

    # The payer is not a receiver. Listing them in both places would bill them
    # for a share they're already absorbing, and double-count them in tallies.
    if config.payee.phone and config.payee.phone in {
        r.phone for r in config.receivers
    }:
        raise ConfigError(
            f"payee.phone {config.payee.phone!r} is also in receivers. The payer "
            "is not a receiver - they carry a share via payee.share but are "
            "never billed. Remove them from the receivers list."
        )

    # Sending for real needs somewhere to send from. Catching this at synth
    # time beats a runtime failure on the first live bill.
    if not config.behavior.dry_run and not config.messaging.origination_number_id:
        raise ConfigError(
            "messaging.origination_number_id: required when behavior.dry_run is "
            "false. Provision and verify a toll-free number first, or keep "
            "dry_run: true to log messages instead of sending them."
        )
    return config


def _reject_duplicates(values: list[str], where: str, field_name: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ConfigError(f"{where}: duplicate {field_name} {value!r}")
        seen.add(value)


def load_config(path: Path | None = None) -> DeployConfig:
    """Load config.yaml, or return defaults if it doesn't exist.

    A missing config.yaml still raises, because there are no sensible defaults
    for who to text or who gets paid. Copy config.example.yaml to get started.
    """
    config_path = path or _CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(
            f"{config_path} not found. Copy config.example.yaml to config.yaml "
            "and fill in your household's details."
        )

    with config_path.open() as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, Mapping):
        raise ConfigError(f"{config_path}: must be a YAML mapping at the top level")

    return _parse(loaded)
