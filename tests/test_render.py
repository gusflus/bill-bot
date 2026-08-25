"""Tests for message rendering, Venmo links, and the Zelle line."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from render import bill_values, render, tally_values, venmo_link, zelle_line


# --- Venmo links -------------------------------------------------------------


def test_venmo_link_carries_amount_and_recipient():
    link = venmo_link("gus-handle", Decimal("23.76"), "SoCalGas bill split")
    query = parse_qs(urlparse(link).query)
    assert urlparse(link).netloc == "venmo.com"
    assert query["txn"] == ["pay"]
    assert query["recipients"] == ["gus-handle"]
    assert query["amount"] == ["23.76"]
    assert query["note"] == ["SoCalGas bill split"]
    assert query["audience"] == ["private"]


def test_venmo_link_percent_encodes_the_note():
    link = venmo_link("h", 5, "PG&E bill split")
    assert "PG%26E" in link
    assert parse_qs(urlparse(link).query)["note"] == ["PG&E bill split"]


def test_venmo_amount_always_two_decimals():
    assert "amount=5.00" in venmo_link("h", 5, "n")
    assert "amount=5.00" in venmo_link("h", Decimal("5.0"), "n")
    assert "amount=1234.50" in venmo_link("h", Decimal("1234.5"), "n")


def test_venmo_link_requires_a_username():
    with pytest.raises(ValueError, match="venmo username is required"):
        venmo_link("", 10, "note")


# --- the Venmo note comes from a template -------------------------------------


def note_of(link: str) -> str:
    return parse_qs(urlparse(link).query)["note"][0]


def test_note_defaults_to_the_biller_name():
    values = bill_values(
        biller="SoCalGas", total="142.53", amount="23.76", label="Sam",
        due_date="Sep 11", month="2026-08",
        venmo_username="h", zelle_contact=None,
    )
    assert note_of(values["venmo_link"]) == "SoCalGas bill split"


def test_note_template_is_rendered_with_bill_values():
    values = bill_values(
        biller="PG&E", total="142.53", amount="23.76", label="Sam",
        due_date="Sep 8", month="2026-08",
        venmo_username="h", zelle_contact=None,
        venmo_note="{month} {biller} - {label} owes {amount} of {total}",
    )
    assert note_of(values["venmo_link"]) == (
        "2026-08 PG&E - Sam owes $23.76 of $142.53"
    )


def test_note_with_no_placeholders_is_used_verbatim():
    values = bill_values(
        biller="PG&E", total="1", amount="1", label="Sam", due_date="x",
        month="2026-08", venmo_username="h", zelle_contact=None,
        venmo_note="utilities",
    )
    assert note_of(values["venmo_link"]) == "utilities"


def test_note_is_percent_encoded_in_the_link():
    values = bill_values(
        biller="PG&E", total="1", amount="1", label="Sam", due_date="x",
        month="2026-08", venmo_username="h", zelle_contact=None,
        venmo_note="{biller} & water",
    )
    assert "PG%26E+%26+water" in values["venmo_link"]
    assert note_of(values["venmo_link"]) == "PG&E & water"


def test_unknown_note_placeholder_raises():
    with pytest.raises(KeyError):
        bill_values(
            biller="PG&E", total="1", amount="1", label="Sam", due_date="x",
            month="2026-08", venmo_username="h", zelle_contact=None,
            venmo_note="{nonsense}",
        )


# --- Zelle line --------------------------------------------------------------


def test_zelle_line_states_amount_and_contact():
    # No link is possible - Zelle publishes no deep-link scheme.
    assert zelle_line("pay@example.com", Decimal("23.76")) == (
        "\nOr Zelle $23.76 to pay@example.com"
    )


@pytest.mark.parametrize("contact", [None, ""])
def test_zelle_line_empty_when_unconfigured(contact):
    assert zelle_line(contact, 10) == ""


def test_zelle_line_composes_cleanly_after_venmo_link():
    """'{venmo_link}{zelle_line}' must read correctly either way."""
    values = bill_values(
        biller="SoCalGas",
        total="100.00",
        amount="25.00",
        label="Gus",
        due_date="Sep 12",
        month="2026-08",
        venmo_username="h",
        zelle_contact=None,
    )
    assert render("Pay: {venmo_link}{zelle_line}", values).endswith("bill+split")

    with_zelle = bill_values(
        biller="SoCalGas",
        total="100.00",
        amount="25.00",
        label="Gus",
        due_date="Sep 12",
        month="2026-08",
        venmo_username="h",
        zelle_contact="pay@example.com",
    )
    rendered = render("Pay: {venmo_link}{zelle_line}", with_zelle)
    assert rendered.splitlines()[-1] == "Or Zelle $25.00 to pay@example.com"


# --- value builders ----------------------------------------------------------


def test_bill_values_formats_money():
    values = bill_values(
        biller="PG&E",
        total=Decimal("1234.5"),
        amount=Decimal("205.75"),
        label="Alex",
        due_date="Sep 12",
        month="2026-08",
        venmo_username="h",
        zelle_contact=None,
    )
    assert values["total"] == "$1,234.50"
    assert values["amount"] == "$205.75"
    assert values["biller"] == "PG&E"
    assert values["label"] == "Alex"


def test_tally_values_stringifies_counts():
    values = tally_values(biller="PG&E", month="2026-08", paid_count=4, receiver_count=6)
    assert values["paid_count"] == "4"
    assert values["receiver_count"] == "6"
    assert "amount" not in values


def test_tally_values_includes_optionals_when_given():
    values = tally_values(
        biller="PG&E",
        month="2026-08",
        paid_count=1,
        receiver_count=6,
        total="100.00",
        label="Gus",
        amount="25.00",
    )
    assert values["total"] == "$100.00"
    assert values["label"] == "Gus"
    assert values["amount"] == "$25.00"


# --- rendering ---------------------------------------------------------------


def test_renders_the_default_bill_template():
    values = bill_values(
        biller="SoCalGas",
        total="142.53",
        amount="23.76",
        label="Gus",
        due_date="Sep 12",
        month="2026-08",
        venmo_username="gus-handle",
        zelle_contact="pay@example.com",
    )
    rendered = render(
        "{biller} bill received: {total} total.\n"
        "Your share, {label}: {amount} - due {due_date}\n"
        "Pay: {venmo_link}{zelle_line}",
        values,
    )
    assert rendered.startswith("SoCalGas bill received: $142.53 total.")
    assert "Your share, Gus: $23.76 - due Sep 12" in rendered
    assert "https://venmo.com/?txn=pay" in rendered
    assert "Or Zelle $23.76 to pay@example.com" in rendered
    assert "{" not in rendered


def test_template_with_no_placeholders_passes_through():
    assert render("A bill arrived.", {}) == "A bill arrived."


def test_missing_placeholder_raises_rather_than_leaking_braces():
    with pytest.raises(KeyError, match="wasn't supplied"):
        render("You owe {amount}", {"biller": "PG&E"})


def test_positional_placeholder_raises():
    with pytest.raises(ValueError, match="positional placeholder"):
        render("You owe {}", {"amount": "$1.00"})
