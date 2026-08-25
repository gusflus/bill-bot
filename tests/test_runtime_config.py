"""Tests for RuntimeConfig, especially its fail-safe defaults."""

from __future__ import annotations

import json

import pytest

from runtime_config import RuntimeConfig

MESSAGES = {"bill": "{amount}", "status": "{paid_count}"}
PAYER = "+15551230000"


def env(**overrides) -> dict[str, str]:
    base = {
        "TABLE_NAME": "bill-bot-test",
        "BEDROCK_MODEL_ID": "test-model",
        "TIMEZONE": "America/Los_Angeles",
        "DRY_RUN": "true",
        "MESSAGES": json.dumps(MESSAGES),
    }
    return {**base, **overrides}


def test_reads_the_environment():
    config = RuntimeConfig.from_env(env(VENMO_USERNAME="gus"))
    assert config.table_name == "bill-bot-test"
    assert config.timezone == "America/Los_Angeles"
    assert config.venmo_username == "gus"
    assert config.messages == MESSAGES


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("FALSE", False), ("False", False), ("true", True)],
)
def test_dry_run_parsing(value, expected):
    assert RuntimeConfig.from_env(env(DRY_RUN=value)).dry_run is expected


@pytest.mark.parametrize("value", ["", "0", "no", "maybe", "tru"])
def test_anything_but_explicit_false_stays_in_dry_run(value):
    """A typo in DRY_RUN must not start texting people."""
    assert RuntimeConfig.from_env(env(DRY_RUN=value)).dry_run is True


def test_missing_dry_run_defaults_to_dry_run():
    e = env()
    del e["DRY_RUN"]
    assert RuntimeConfig.from_env(e).dry_run is True


def test_blank_zelle_contact_becomes_none():
    """So render() drops the Zelle line rather than printing an empty handle."""
    assert RuntimeConfig.from_env(env(ZELLE_CONTACT="")).zelle_contact is None
    assert RuntimeConfig.from_env(env(ZELLE_CONTACT="a@b.c")).zelle_contact == "a@b.c"


def test_missing_table_name_fails_loudly():
    e = env()
    del e["TABLE_NAME"]
    with pytest.raises(KeyError, match="TABLE_NAME"):
        RuntimeConfig.from_env(e)


def test_template_lookup():
    config = RuntimeConfig.from_env(env())
    assert config.template("bill") == "{amount}"


def test_missing_template_names_what_is_available():
    config = RuntimeConfig.from_env(env())
    with pytest.raises(KeyError, match="reminder"):
        config.template("reminder")


def test_defaults_are_sane_when_optional_vars_are_absent():
    config = RuntimeConfig.from_env(
        {"TABLE_NAME": "t", "MESSAGES": json.dumps(MESSAGES)}
    )
    assert config.timezone == "UTC"
    assert config.on_low_confidence == "send"
    assert config.record_ttl_days == 400
    assert config.dry_run is True
    assert config.origination_number_id == ""


def test_is_immutable():
    config = RuntimeConfig.from_env(env())
    with pytest.raises(Exception):
        config.dry_run = False  # type: ignore[misc]
