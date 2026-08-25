"""Tests for the config seeder: upsert, update, prune, and data safety."""

from __future__ import annotations

import json
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from tests.conftest import load_handler

seed = load_handler("seed_config")


def props(receivers=None, senders=None) -> dict[str, str]:
    return {
        "Receivers": json.dumps(
            receivers
            if receivers is not None
            else [{"label": "Gus", "phone": "+15551230001", "share": 1}]
        ),
        "Senders": json.dumps(
            senders
            if senders is not None
            else [
                {"id": "pg-e", "name": "PG&E", "from_address": "billpay.pge.com"}
            ]
        ),
    }


def config_rows(table) -> dict[str, dict]:
    response = table.query(KeyConditionExpression=Key("PK").eq("CONFIG"))
    return {item["SK"]: item for item in response["Items"]}


# --- seeding ------------------------------------------------------------------


def test_seeds_receivers_and_senders(state_table):
    counts = seed.sync_config(state_table, props())
    assert counts == {"receivers": 1, "senders": 1, "pruned": 0}

    rows = config_rows(state_table)
    assert set(rows) == {"RECEIVER#+15551230001", "SENDER#billpay.pge.com"}

    receiver = rows["RECEIVER#+15551230001"]
    assert receiver["label"] == "Gus"
    assert receiver["share"] == Decimal("1")
    assert receiver["active"] is True

    sender = rows["SENDER#billpay.pge.com"]
    assert sender["id"] == "pg-e"
    assert sender["name"] == "PG&E"


def test_fractional_share_survives_the_roundtrip(state_table):
    """Stored as Decimal, not float, so 2.5 comes back exactly."""
    seed.sync_config(
        state_table,
        props(receivers=[{"label": "Alex", "phone": "+15551230003", "share": 2.5}]),
    )
    row = config_rows(state_table)["RECEIVER#+15551230003"]
    assert row["share"] == Decimal("2.5")


def test_seeding_is_idempotent(state_table):
    seed.sync_config(state_table, props())
    counts = seed.sync_config(state_table, props())
    assert counts == {"receivers": 1, "senders": 1, "pruned": 0}
    assert len(config_rows(state_table)) == 2


# --- updating -----------------------------------------------------------------


def test_changing_a_share_updates_in_place(state_table):
    seed.sync_config(state_table, props())
    seed.sync_config(
        state_table,
        props(receivers=[{"label": "Gus", "phone": "+15551230001", "share": 3}]),
    )
    rows = config_rows(state_table)
    assert len(rows) == 2
    assert rows["RECEIVER#+15551230001"]["share"] == Decimal("3")


def test_renaming_a_label_updates_in_place(state_table):
    seed.sync_config(state_table, props())
    seed.sync_config(
        state_table,
        props(receivers=[{"label": "Gustav", "phone": "+15551230001", "share": 1}]),
    )
    assert config_rows(state_table)["RECEIVER#+15551230001"]["label"] == "Gustav"


# --- pruning ------------------------------------------------------------------


def test_removing_a_receiver_from_config_prunes_the_row(state_table):
    seed.sync_config(
        state_table,
        props(
            receivers=[
                {"label": "Gus", "phone": "+15551230001", "share": 1},
                {"label": "Departed", "phone": "+15559999999", "share": 1},
            ]
        ),
    )
    assert len(config_rows(state_table)) == 3

    counts = seed.sync_config(state_table, props())
    assert counts["pruned"] == 1
    assert set(config_rows(state_table)) == {
        "RECEIVER#+15551230001",
        "SENDER#billpay.pge.com",
    }


def test_removing_a_sender_from_config_prunes_the_row(state_table):
    seed.sync_config(
        state_table,
        props(
            senders=[
                {"id": "pg-e", "name": "PG&E", "from_address": "billpay.pge.com"},
                {"id": "old", "name": "Old", "from_address": "old.example.com"},
            ]
        ),
    )
    seed.sync_config(state_table, props())
    assert "SENDER#old.example.com" not in config_rows(state_table)


def test_changing_a_phone_number_prunes_the_old_row(state_table):
    """The phone is the key, so a correction must not leave a ghost row."""
    seed.sync_config(state_table, props())
    seed.sync_config(
        state_table,
        props(receivers=[{"label": "Gus", "phone": "+15551230002", "share": 1}]),
    )
    rows = config_rows(state_table)
    assert "RECEIVER#+15551230001" not in rows
    assert "RECEIVER#+15551230002" in rows


# --- data safety --------------------------------------------------------------


def test_seeding_never_touches_bill_or_payment_rows(state_table):
    """A deploy must not disturb runtime data. This is the important one."""
    state_table.put_item(
        Item={
            "PK": "BILL#pg-e#2026-08",
            "SK": "META",
            "total": Decimal("142.53"),
            "status": "notified",
        }
    )
    state_table.put_item(
        Item={
            "PK": "BILL#pg-e#2026-08",
            "SK": "PAY#+15551230001",
            "paid": True,
            "amount_owed": Decimal("23.76"),
        }
    )

    # Seed twice, including a prune, to exercise the delete path.
    seed.sync_config(
        state_table,
        props(
            receivers=[
                {"label": "Gus", "phone": "+15551230001", "share": 1},
                {"label": "Gone", "phone": "+15558888888", "share": 1},
            ]
        ),
    )
    seed.sync_config(state_table, props())

    bill = state_table.query(KeyConditionExpression=Key("PK").eq("BILL#pg-e#2026-08"))
    assert len(bill["Items"]) == 2
    meta = next(i for i in bill["Items"] if i["SK"] == "META")
    assert meta["total"] == Decimal("142.53")
    assert meta["status"] == "notified"
    payment = next(i for i in bill["Items"] if i["SK"].startswith("PAY#"))
    assert payment["paid"] is True


# --- custom resource lifecycle ------------------------------------------------


def test_create_event_seeds_and_reports_counts(state_table):
    result = seed.handler(
        {"RequestType": "Create", "ResourceProperties": props()}, None
    )
    assert result["PhysicalResourceId"] == "bill-bot-config-seed"
    assert result["Data"] == {"receivers": "1", "senders": "1", "pruned": "0"}


def test_update_event_reseeds(state_table):
    seed.handler({"RequestType": "Create", "ResourceProperties": props()}, None)
    result = seed.handler(
        {
            "RequestType": "Update",
            "ResourceProperties": props(
                receivers=[
                    {"label": "Gus", "phone": "+15551230001", "share": 1},
                    {"label": "Sam", "phone": "+15551230002", "share": 2},
                ]
            ),
        },
        None,
    )
    assert result["Data"]["receivers"] == "2"
    assert config_rows(state_table)["RECEIVER#+15551230002"]["share"] == Decimal("2")


def test_delete_event_leaves_data_alone(state_table):
    """cdk destroy must not take the payment ledger with it."""
    seed.handler({"RequestType": "Create", "ResourceProperties": props()}, None)
    result = seed.handler({"RequestType": "Delete", "ResourceProperties": {}}, None)
    assert result["PhysicalResourceId"] == "bill-bot-config-seed"
    assert len(config_rows(state_table)) == 2
