"""Turning a bill plus a receiver into the text they actually get.

Every outbound string is a template from config.yaml, so this module only
supplies values and substitutes them. Placeholders were already validated at
deploy time (see infra/config.py), which is why rendering can afford to treat an
unknown placeholder as a hard error here.

Pure functions, no dependencies.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlencode

from shares import format_money

VENMO_BASE = "https://venmo.com/"


def venmo_link(username: str, amount: Decimal | int | str, note: str) -> str:
    """A pre-filled Venmo payment link for one roommate's share.

    Venmo removed programmatic charging for personal accounts, so this is as far
    as automation goes: tapping it opens Venmo with the amount, recipient, and
    comment filled in, ready to send. Nothing is charged automatically.

    ``note`` becomes the payment comment, which both parties see in their Venmo
    history afterwards - so it's worth naming the biller rather than leaving it
    generic.
    """
    if not username:
        raise ValueError("venmo username is required to build a payment link")
    params = {
        "txn": "pay",
        "audience": "private",
        "recipients": username,
        "amount": f"{Decimal(str(amount)):.2f}",
        "note": note,
    }
    return f"{VENMO_BASE}?{urlencode(params)}"


def zelle_line(contact: str | None, amount: Decimal | int | str) -> str:
    """Zelle instructions as plain text, or '' when no contact is configured.

    Zelle publishes no deep-link or request URL scheme, so unlike Venmo there is
    nothing to link to - the amount and handle can only be stated.

    Returns a leading newline so a template can write '{venmo_link}{zelle_line}'
    and get sensible output whether or not Zelle is configured.
    """
    if not contact:
        return ""
    return f"\nOr Zelle {format_money(amount)} to {contact}"


def bill_values(
    *,
    biller: str,
    total: Decimal | int | str,
    amount: Decimal | int | str,
    label: str,
    due_date: str,
    month: str,
    venmo_username: str,
    zelle_contact: str | None,
    venmo_note: str = "{biller} bill split",
) -> dict[str, str]:
    """Placeholder values for a per-receiver message (bill, reminder).

    ``venmo_note`` is itself a template, rendered first and then embedded in the
    payment link as the Venmo comment.
    """
    note = render(
        venmo_note,
        {
            "biller": biller,
            "label": label,
            "amount": format_money(amount),
            "total": format_money(total),
            "month": month,
            "due_date": due_date,
        },
    )
    return {
        "biller": biller,
        "total": format_money(total),
        "amount": format_money(amount),
        "label": label,
        "due_date": due_date,
        "month": month,
        "venmo_link": venmo_link(venmo_username, amount, note),
        "zelle_line": zelle_line(zelle_contact, amount),
    }


def tally_values(
    *,
    biller: str,
    month: str,
    paid_count: int,
    receiver_count: int,
    total: Decimal | int | str | None = None,
    label: str | None = None,
    amount: Decimal | int | str | None = None,
) -> dict[str, str]:
    """Placeholder values for a reply message (paid_ack, status)."""
    values = {
        "biller": biller,
        "month": month,
        "paid_count": str(paid_count),
        "receiver_count": str(receiver_count),
    }
    if total is not None:
        values["total"] = format_money(total)
    if label is not None:
        values["label"] = label
    if amount is not None:
        values["amount"] = format_money(amount)
    return values


def render(template: str, values: dict[str, str]) -> str:
    """Substitute ``values`` into ``template``.

    Raises on an unknown or missing placeholder rather than emitting a message
    with a literal '{amount}' in it. Deploy-time validation should already have
    caught this, so reaching here means the two got out of sync.
    """
    try:
        return template.format(**values)
    except KeyError as exc:
        raise KeyError(
            f"template needs placeholder {exc} but it wasn't supplied; "
            f"available: {sorted(values)}"
        ) from exc
    except IndexError as exc:
        raise ValueError(
            f"template has a positional placeholder like '{{}}': {template!r}"
        ) from exc
