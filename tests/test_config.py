"""Tests for the config.yaml loader and its validation rules."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from infra.config import (
    ConfigError,
    DeployConfig,
    load_config,
    render_sample,
    segment_count,
    slugify,
)

MINIMAL = """
payee:
  venmo_username: someone
receivers:
  - { label: Gus, phone: "+15551230001", share: 1 }
senders:
  - { name: "PG&E", from_address: billpay.pge.com }
"""


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def load(tmp_path: Path, body: str) -> DeployConfig:
    return load_config(write_config(tmp_path, body))


# --- happy path ---------------------------------------------------------------


def test_minimal_config_parses(tmp_path):
    config = load(tmp_path, MINIMAL)
    assert config.payee.venmo_username == "someone"
    assert len(config.receivers) == 1
    assert config.receivers[0].label == "Gus"
    assert config.senders[0].id == "pg-e"
    # Defaults fill in everything else.
    assert config.behavior.dry_run is True
    assert config.messaging.channel == "sms"
    assert config.stack_name == "bill-bot-home-stack"


def test_repo_example_config_is_valid():
    """config.example.yaml must always be loadable - it's the onboarding path."""
    example = Path(__file__).parent.parent / "config.example.yaml"
    config = load_config(example)
    assert len(config.receivers) == 3
    assert len(config.senders) == 4
    # 1 + 2 + 1 from receivers, plus the payer's 1.
    assert config.total_share_weight() == 5.0


def test_weights_and_zelle_are_read(tmp_path):
    config = load(
        tmp_path,
        """
        payee:
          venmo_username: someone
          zelle_contact: pay@example.com
          share: 0
        receivers:
          - { label: Gus, phone: "+15551230001", share: 1 }
          - { label: Alex, phone: "+15551230002", share: 2.5 }
        senders:
          - { name: SoCalGas, from_address: socalgas.com }
        """,
    )
    assert config.total_share_weight() == 3.5
    assert config.payee.zelle_contact == "pay@example.com"


def test_sender_id_can_be_overridden(tmp_path):
    config = load(
        tmp_path,
        MINIMAL.replace(
            '{ name: "PG&E", from_address: billpay.pge.com }',
            '{ name: "PG&E", from_address: billpay.pge.com, id: legacy-pge }',
        ),
    )
    assert config.senders[0].id == "legacy-pge"


def test_missing_file_names_the_remedy(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config(tmp_path / "nope.yaml")


# --- structural validation ----------------------------------------------------


def test_unknown_top_level_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"unknown key\(s\) \['nonsense'\]"):
        load(tmp_path, MINIMAL + "\nnonsense: 1\n")


def test_unknown_nested_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"behavior: unknown key\(s\) \['dryrun'\]"):
        load(tmp_path, MINIMAL + "\nbehavior:\n  dryrun: true\n")


def test_unknown_receiver_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"receivers\[0\]: unknown key"):
        load(
            tmp_path,
            """
            payee: { venmo_username: someone }
            receivers:
              - { label: Gus, phone: "+15551230001", weight: 1 }
            senders:
              - { name: X, from_address: x.com }
            """,
        )


def test_top_level_must_be_mapping(tmp_path):
    with pytest.raises(ConfigError, match="mapping at the top level"):
        load(tmp_path, "- just\n- a\n- list\n")


def test_receivers_must_be_a_list(tmp_path):
    with pytest.raises(ConfigError, match="receivers: expected a list"):
        load(
            tmp_path,
            """
            payee: { venmo_username: someone }
            receivers: { label: Gus }
            senders:
              - { name: X, from_address: x.com }
            """,
        )


# --- required fields ----------------------------------------------------------


def test_empty_receivers_rejected(tmp_path):
    with pytest.raises(ConfigError, match="receivers: at least one"):
        load(
            tmp_path,
            """
            payee: { venmo_username: someone }
            receivers: []
            senders:
              - { name: X, from_address: x.com }
            """,
        )


def test_empty_senders_rejected(tmp_path):
    with pytest.raises(ConfigError, match="senders: at least one"):
        load(
            tmp_path,
            """
            payee: { venmo_username: someone }
            receivers:
              - { label: Gus, phone: "+15551230001" }
            senders: []
            """,
        )


def test_missing_venmo_username_rejected(tmp_path):
    with pytest.raises(ConfigError, match="payee.venmo_username: required"):
        load(
            tmp_path,
            """
            payee: {}
            receivers:
              - { label: Gus, phone: "+15551230001" }
            senders:
              - { name: X, from_address: x.com }
            """,
        )


def test_venmo_username_with_at_sign_rejected(tmp_path):
    with pytest.raises(ConfigError, match="drop the leading '@'"):
        load(tmp_path, MINIMAL.replace("venmo_username: someone", 'venmo_username: "@someone"'))


def test_receiver_missing_phone_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"receivers\[0\]: missing required"):
        load(
            tmp_path,
            """
            payee: { venmo_username: someone }
            receivers:
              - { label: Gus }
            senders:
              - { name: X, from_address: x.com }
            """,
        )


# --- phone numbers ------------------------------------------------------------


@pytest.mark.parametrize(
    "phone",
    ["5551230001", "+0551230001", "+1555", "555-123-0001", "+1 555 123 0001", ""],
)
def test_non_e164_phone_rejected(tmp_path, phone):
    with pytest.raises(ConfigError, match=r"receivers\[0\]\.phone"):
        load(tmp_path, MINIMAL.replace('"+15551230001"', f'"{phone}"'))


def test_duplicate_phone_rejected(tmp_path):
    with pytest.raises(ConfigError, match="duplicate phone"):
        load(
            tmp_path,
            """
            payee: { venmo_username: someone }
            receivers:
              - { label: Gus, phone: "+15551230001" }
              - { label: Typo, phone: "+15551230001" }
            senders:
              - { name: X, from_address: x.com }
            """,
        )


# --- share weights ------------------------------------------------------------


@pytest.mark.parametrize("share", [0, -1, -0.5])
def test_non_positive_share_rejected(tmp_path, share):
    with pytest.raises(ConfigError, match=r"receivers\[0\]\.share.*greater than 0"):
        load(tmp_path, MINIMAL.replace("share: 1", f"share: {share}"))


def test_non_numeric_share_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"receivers\[0\]\.share: must be a number"):
        load(tmp_path, MINIMAL.replace("share: 1", 'share: "1"'))


# --- senders ------------------------------------------------------------------


def test_duplicate_from_address_rejected(tmp_path):
    with pytest.raises(ConfigError, match="duplicate from_address"):
        load(
            tmp_path,
            """
            payee: { venmo_username: someone }
            receivers:
              - { label: Gus, phone: "+15551230001" }
            senders:
              - { name: One, from_address: pge.com }
              - { name: Two, from_address: PGE.com }
            """,
        )


def test_duplicate_derived_sender_id_rejected(tmp_path):
    """'PG&E' and 'PG E' both slug to 'pg-e' - that would collide in DynamoDB."""
    with pytest.raises(ConfigError, match="duplicate id"):
        load(
            tmp_path,
            """
            payee: { venmo_username: someone }
            receivers:
              - { label: Gus, phone: "+15551230001" }
            senders:
              - { name: "PG&E", from_address: a.com }
              - { name: "PG E", from_address: b.com }
            """,
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [("PG&E", "pg-e"), ("SoCalGas", "socalgas"), ("Water / Sewer", "water-sewer")],
)
def test_slugify(name, expected):
    assert slugify(name) == expected


# --- message templates --------------------------------------------------------


def test_unknown_placeholder_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"messages\.bill: unknown placeholder"):
        load(tmp_path, MINIMAL + '\nmessages:\n  bill: "Pay {nonsense} now"\n')


def test_error_lists_available_placeholders(tmp_path):
    with pytest.raises(ConfigError, match=r"\{venmo_link\}"):
        load(tmp_path, MINIMAL + '\nmessages:\n  bill: "Pay {nonsense} now"\n')


def test_placeholder_valid_elsewhere_still_rejected_out_of_context(tmp_path):
    """{amount} has no meaning in 'status', which isn't per-receiver."""
    with pytest.raises(ConfigError, match=r"messages\.status: unknown placeholder"):
        load(tmp_path, MINIMAL + '\nmessages:\n  status: "You owe {amount}"\n')


def test_paid_count_rejected_in_bill(tmp_path):
    """No tally exists yet when the bill first goes out."""
    with pytest.raises(ConfigError, match=r"messages\.bill: unknown placeholder"):
        load(tmp_path, MINIMAL + '\nmessages:\n  bill: "{paid_count} paid"\n')


def test_empty_bill_template_rejected(tmp_path):
    with pytest.raises(ConfigError, match="messages.bill: cannot be empty"):
        load(tmp_path, MINIMAL + '\nmessages:\n  bill: "   "\n')


def test_non_string_template_rejected(tmp_path):
    with pytest.raises(ConfigError, match="messages.bill: expected a string"):
        load(tmp_path, MINIMAL + "\nmessages:\n  bill: 42\n")


def test_templates_with_no_placeholders_are_fine(tmp_path):
    config = load(tmp_path, MINIMAL + '\nmessages:\n  bill: "A bill arrived."\n')
    assert config.messages.bill == "A bill arrived."


# --- segment counting ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("a", 1),
        ("a" * 160, 1),
        ("a" * 161, 2),
        ("a" * 306, 2),
        ("a" * 307, 3),
    ],
)
def test_gsm7_segment_boundaries(text, expected):
    assert segment_count(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\u26a0", 1),
        ("\u26a0" + "a" * 69, 1),
        ("\u26a0" + "a" * 70, 2),
    ],
)
def test_non_gsm7_drops_to_ucs2_budget(text, expected):
    """One emoji cuts the per-segment budget from 160 to 70."""
    assert segment_count(text) == expected


def test_gsm7_extension_chars_cost_two_septets():
    # 80 braces = 160 septets = still one segment.
    assert segment_count("{" * 80) == 1
    assert segment_count("{" * 81) == 2


def test_segment_estimate_for_default_bill_template(tmp_path):
    config = load(tmp_path, MINIMAL)
    estimates = config.messages.segment_estimates()
    # The default bill template plus a ~90-char Venmo link lands over one
    # segment - exactly the cost surprise max_segments_warn exists to surface.
    assert estimates["bill"] == 2
    assert estimates["status"] == 1
    assert set(estimates) == {
        "bill", "paid_ack", "status", "reminder", "payment_alert",
    }


def test_render_sample_fills_every_allowed_placeholder():
    rendered = render_sample("bill", "{biller} {total} {amount} {label} {due_date} "
                             "{month} {venmo_link} {zelle_line}")
    assert "{" not in rendered


# --- behavior and messaging ---------------------------------------------------


def test_invalid_on_low_confidence_rejected(tmp_path):
    with pytest.raises(ConfigError, match="must be 'send' or 'hold'"):
        load(tmp_path, MINIMAL + "\nbehavior:\n  on_low_confidence: maybe\n")


def test_non_bool_dry_run_rejected(tmp_path):
    with pytest.raises(ConfigError, match="behavior.dry_run: must be true or false"):
        load(tmp_path, MINIMAL + '\nbehavior:\n  dry_run: "yes"\n')


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_lookback_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="behavior.lookback_days"):
        load(tmp_path, MINIMAL + f"\nbehavior:\n  lookback_days: {value}\n")


def test_identical_labels_rejected(tmp_path):
    with pytest.raises(ConfigError, match="must differ"):
        load(
            tmp_path,
            MINIMAL
            + "\nbehavior:\n  processed_label: Same\n  error_label: Same\n",
        )


def test_non_sms_channel_rejected(tmp_path):
    with pytest.raises(ConfigError, match="only 'sms' is supported"):
        load(tmp_path, MINIMAL + "\nmessaging:\n  channel: rcs\n")


def test_live_send_requires_origination_number(tmp_path):
    with pytest.raises(ConfigError, match="origination_number_id: required"):
        load(tmp_path, MINIMAL + "\nbehavior:\n  dry_run: false\n")


def test_live_send_allowed_with_origination_number(tmp_path):
    config = load(
        tmp_path,
        MINIMAL
        + "\nbehavior:\n  dry_run: false\n"
        + "\nmessaging:\n  origination_number_id: phone-abc123\n",
    )
    assert config.behavior.dry_run is False
    assert config.messaging.origination_number_id == "phone-abc123"


def test_dry_run_does_not_require_origination_number(tmp_path):
    config = load(tmp_path, MINIMAL)
    assert config.messaging.origination_number_id == ""


# --- reminders ----------------------------------------------------------------


def test_reminders_default_to_off(tmp_path):
    """Texting people about money should be opted into deliberately."""
    config = load(tmp_path, MINIMAL)
    assert config.reminders.enabled is False
    assert config.reminders.after_days == 3
    assert config.reminders.repeat_days == 3


def test_reminders_can_be_enabled(tmp_path):
    config = load(
        tmp_path,
        MINIMAL + "\nreminders:\n  enabled: true\n  after_days: 7\n  hour_utc: 9\n",
    )
    assert config.reminders.enabled is True
    assert config.reminders.after_days == 7
    assert config.reminders.hour_utc == 9


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_reminder_days_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="reminders.after_days"):
        load(tmp_path, MINIMAL + f"\nreminders:\n  after_days: {value}\n")


@pytest.mark.parametrize("value", [-1, 24, 99])
def test_out_of_range_reminder_hour_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="reminders.hour_utc"):
        load(tmp_path, MINIMAL + f"\nreminders:\n  hour_utc: {value}\n")


def test_unknown_reminder_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"reminders: unknown key"):
        load(tmp_path, MINIMAL + "\nreminders:\n  every_days: 3\n")


# --- the payer ----------------------------------------------------------------


def test_payer_defaults(tmp_path):
    config = load(tmp_path, MINIMAL)
    assert config.payee.label == "Me"
    assert config.payee.phone is None
    assert config.payee.share == 1.0
    assert config.payee.notify_on_payment is False


def test_payer_share_counts_toward_the_split(tmp_path):
    """Otherwise receivers would be overcharged to cover the payer's portion."""
    config = load(
        tmp_path,
        """
        payee: { venmo_username: someone, share: 1 }
        receivers:
          - { label: Sam, phone: "+15551230002", share: 1 }
          - { label: Alex, phone: "+15551230003", share: 2 }
        senders:
          - { name: X, from_address: x.com }
        """,
    )
    assert config.total_share_weight() == 4.0
    assert config.payee.in_split


def test_payer_share_zero_excludes_them_from_the_split(tmp_path):
    config = load(tmp_path, MINIMAL + "\npayee:\n  venmo_username: x\n  share: 0\n")
    assert config.total_share_weight() == 1.0
    assert not config.payee.in_split


def test_negative_payer_share_rejected(tmp_path):
    with pytest.raises(ConfigError, match="payee.share: cannot be negative"):
        load(tmp_path, MINIMAL + "\npayee:\n  venmo_username: x\n  share: -1\n")


def test_payer_phone_must_be_e164(tmp_path):
    with pytest.raises(ConfigError, match="payee.phone"):
        load(
            tmp_path,
            MINIMAL + '\npayee:\n  venmo_username: x\n  phone: "555-1234"\n',
        )


def test_notify_on_payment_requires_a_phone(tmp_path):
    with pytest.raises(ConfigError, match="needs payee.phone set"):
        load(
            tmp_path,
            MINIMAL + "\npayee:\n  venmo_username: x\n  notify_on_payment: true\n",
        )


def test_notify_on_payment_with_a_phone_is_accepted(tmp_path):
    config = load(
        tmp_path,
        MINIMAL
        + '\npayee:\n  venmo_username: x\n  phone: "+15551230009"\n'
        + "  notify_on_payment: true\n",
    )
    assert config.payee.notify_on_payment is True
    assert config.payee.phone == "+15551230009"


def test_payer_listed_as_a_receiver_is_rejected(tmp_path):
    """Would bill them for a share they're already absorbing."""
    with pytest.raises(ConfigError, match="also in receivers"):
        load(
            tmp_path,
            """
            payee:
              venmo_username: x
              phone: "+15551230001"
            receivers:
              - { label: Gus, phone: "+15551230001", share: 1 }
            senders:
              - { name: X, from_address: x.com }
            """,
        )


def test_empty_payer_label_rejected(tmp_path):
    with pytest.raises(ConfigError, match="payee.label: cannot be empty"):
        load(tmp_path, MINIMAL + '\npayee:\n  venmo_username: x\n  label: "  "\n')


# --- payment_alert template ---------------------------------------------------


def test_payment_alert_has_a_default(tmp_path):
    config = load(tmp_path, MINIMAL)
    assert "{label}" in config.messages.payment_alert


def test_payment_alert_rejects_out_of_context_placeholders(tmp_path):
    """No {venmo_link}: the payer isn't being asked to pay anyone."""
    with pytest.raises(ConfigError, match="messages.payment_alert"):
        load(
            tmp_path,
            MINIMAL + '\nmessages:\n  payment_alert: "paid via {venmo_link}"\n',
        )


def test_low_confidence_suffix_is_gone(tmp_path):
    """Removed - low confidence is recorded, not announced to roommates."""
    with pytest.raises(ConfigError, match=r"messages: unknown key"):
        load(tmp_path, MINIMAL + '\nmessages:\n  low_confidence_suffix: "!"\n')


def test_reserved_concurrency_is_gone(tmp_path):
    """Removed - plain Lambdas, no concurrency reservations."""
    with pytest.raises(ConfigError, match=r"behavior: unknown key"):
        load(tmp_path, MINIMAL + "\nbehavior:\n  reserved_concurrency: 5\n")


# --- the Venmo note -----------------------------------------------------------


def test_venmo_note_defaults_to_the_biller_name(tmp_path):
    config = load(tmp_path, MINIMAL)
    assert config.messages.venmo_note == "{biller} bill split"


def test_venmo_note_is_configurable(tmp_path):
    config = load(
        tmp_path,
        MINIMAL + '\nmessages:\n  venmo_note: "{month} {biller} - {amount}"\n',
    )
    assert config.messages.venmo_note == "{month} {biller} - {amount}"


def test_empty_venmo_note_rejected(tmp_path):
    """Venmo payments need a note."""
    with pytest.raises(ConfigError, match="messages.venmo_note: cannot be empty"):
        load(tmp_path, MINIMAL + '\nmessages:\n  venmo_note: "  "\n')


def test_venmo_note_rejects_the_link_placeholder(tmp_path):
    """The note goes *inside* the link, so it can't contain the link."""
    with pytest.raises(ConfigError, match="messages.venmo_note"):
        load(tmp_path, MINIMAL + '\nmessages:\n  venmo_note: "pay {venmo_link}"\n')


def test_venmo_note_is_not_an_sms_of_its_own(tmp_path):
    """It rides inside the Venmo URL, so it gets no segment line."""
    config = load(tmp_path, MINIMAL)
    assert "venmo_note" not in config.messages.segment_estimates()


# --- misc ---------------------------------------------------------------------


def test_bad_timezone_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not a known IANA zone"):
        load(tmp_path, MINIMAL + "\ntimezone: Mars/Olympus_Mons\n")


@pytest.mark.parametrize("suffix", ["Home", "my_home", "a" * 33, ""])
def test_bad_stack_suffix_rejected(tmp_path, suffix):
    with pytest.raises(ConfigError, match="stack_suffix"):
        load(tmp_path, MINIMAL + f'\nstack_suffix: "{suffix}"\n')


def test_config_is_immutable(tmp_path):
    config = load(tmp_path, MINIMAL)
    with pytest.raises(Exception):
        config.behavior.dry_run = False  # type: ignore[misc]

