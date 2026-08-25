"""Tests for the amount sweep, presence check, and HTML fallback."""

from __future__ import annotations

from decimal import Decimal

import pytest

from html_to_text import html_to_text
from validate import amount_appears, find_amounts, largest_amount


# --- finding amounts ---------------------------------------------------------


def test_finds_amounts_in_order():
    text = "Total $1,234.56 due, minimum payment 25.00, late fee $5.00"
    assert find_amounts(text) == [
        Decimal("1234.56"),
        Decimal("25.00"),
        Decimal("5.00"),
    ]


def test_handles_dollar_sign_and_comma_variants():
    assert find_amounts("$142.53") == [Decimal("142.53")]
    assert find_amounts("142.53") == [Decimal("142.53")]
    assert find_amounts("$ 142.53") == [Decimal("142.53")]
    assert find_amounts("$1,234,567.89") == [Decimal("1234567.89")]


def test_keeps_duplicates():
    """A total repeated through a bill is signal, not noise."""
    assert find_amounts("$50.00 ... $50.00") == [Decimal("50.00")] * 2


def test_ignores_things_that_are_not_currency():
    # Two decimal places required, which excludes the noise in these emails.
    assert find_amounts("Account 1234567890") == []
    assert find_amounts("Due 9/12/2026") == []
    assert find_amounts("Call 1-800-555-1212") == []
    assert find_amounts("Version 1.2") == []
    assert find_amounts("usage 1234.5 kWh") == []


def test_empty_and_none_safe():
    assert find_amounts("") == []
    assert find_amounts(None) == []


# --- presence check ----------------------------------------------------------


def test_amount_found_regardless_of_formatting():
    body = "Amount due: $1,234.56 by Sep 12"
    assert amount_appears(body, "1234.56")
    assert amount_appears(body, Decimal("1234.56"))
    assert amount_appears(body, 1234.56)


def test_amount_absent_is_detected():
    """This is the hallucination guard: a number Haiku invented isn't in the text."""
    body = "Amount due: $1,234.56"
    assert not amount_appears(body, "99.99")
    assert not amount_appears(body, "1234.57")


def test_amount_appears_handles_garbage_input():
    assert not amount_appears("$10.00", "not-a-number")
    assert not amount_appears("", "10.00")


def test_trailing_zero_differences_still_match_numerically():
    assert amount_appears("$50.00 due", Decimal("50.0"))
    assert amount_appears("$50.00 due", 50)


# --- largest amount fallback -------------------------------------------------


def test_largest_amount():
    assert largest_amount("min 25.00, total $1,234.56, fee 5.00") == Decimal("1234.56")


def test_largest_amount_none_when_no_amounts():
    assert largest_amount("no money here") is None


# --- HTML fallback -----------------------------------------------------------


def test_html_to_text_extracts_amount_from_table_markup():
    html = """
    <html><head><style>.x{color:red}</style></head>
    <body><table><tr><td>Amount Due</td><td>$142.53</td></tr></table>
    <p>Due&nbsp;Sep 12</p></body></html>
    """
    text = html_to_text(html)
    assert "Amount Due" in text
    assert amount_appears(text, "142.53")
    assert "color:red" not in text


def test_html_to_text_drops_scripts_and_decodes_entities():
    text = html_to_text("<script>var x=1</script><p>Pay AT&amp;T $9.99</p>")
    assert "var x" not in text
    assert "AT&T" in text


def test_html_to_text_breaks_lines_on_block_tags():
    assert html_to_text("<p>one</p><p>two</p>").splitlines() == ["one", "two"]
    assert html_to_text("a<br>b").splitlines() == ["a", "b"]
