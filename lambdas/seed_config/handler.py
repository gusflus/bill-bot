"""Seeds config.yaml's receivers and senders into DynamoDB on every deploy.

Backs a CDK custom resource. CloudFormation hands us the desired state as JSON
strings; we upsert it and delete any CONFIG row that is no longer in config.yaml,
so removing a roommate from the YAML actually removes them from the table.

Only ``PK = CONFIG`` rows are ever touched. Bill and payment rows are runtime
data and a deploy must never disturb them - that's why this prunes by querying
the CONFIG partition rather than scanning the table.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CONFIG_PK = "CONFIG"
RECEIVER_PREFIX = "RECEIVER#"
SENDER_PREFIX = "SENDER#"


def _table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def _desired_items(props: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the full desired CONFIG partition, keyed by sort key."""
    items: dict[str, dict[str, Any]] = {}

    for receiver in json.loads(props["Receivers"]):
        sk = f"{RECEIVER_PREFIX}{receiver['phone']}"
        items[sk] = {
            "PK": CONFIG_PK,
            "SK": sk,
            "phone": receiver["phone"],
            "label": receiver["label"],
            # DynamoDB has no float type; Decimal via str keeps 2.5 exact.
            "share": Decimal(str(receiver["share"])),
            "active": True,
        }

    for sender in json.loads(props["Senders"]):
        sk = f"{SENDER_PREFIX}{sender['from_address']}"
        items[sk] = {
            "PK": CONFIG_PK,
            "SK": sk,
            "id": sender["id"],
            "name": sender["name"],
            "from_address": sender["from_address"],
        }

    return items


def _existing_sort_keys(table) -> set[str]:
    keys: set[str] = set()
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(CONFIG_PK),
        "ProjectionExpression": "SK",
    }
    while True:
        response = table.query(**kwargs)
        keys.update(item["SK"] for item in response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return keys
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def sync_config(table, props: dict[str, Any]) -> dict[str, int]:
    """Upsert desired CONFIG rows and delete the ones that fell out of config."""
    desired = _desired_items(props)
    existing = _existing_sort_keys(table)

    with table.batch_writer() as batch:
        for item in desired.values():
            batch.put_item(Item=item)
        for stale in sorted(existing - set(desired)):
            logger.info("pruning config row no longer in config.yaml: %s", stale)
            batch.delete_item(Key={"PK": CONFIG_PK, "SK": stale})

    receivers = sum(1 for k in desired if k.startswith(RECEIVER_PREFIX))
    senders = sum(1 for k in desired if k.startswith(SENDER_PREFIX))
    pruned = len(existing - set(desired))
    logger.info(
        "config synced: %d receiver(s), %d sender(s), %d pruned",
        receivers,
        senders,
        pruned,
    )
    return {"receivers": receivers, "senders": senders, "pruned": pruned}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_type = event["RequestType"]
    logger.info("custom resource %s", request_type)

    # On Delete, leave the data alone. The table is RETAIN precisely so a
    # `cdk destroy` doesn't take the payment ledger with it.
    if request_type == "Delete":
        return {"PhysicalResourceId": "bill-bot-config-seed"}

    counts = sync_config(_table(), event["ResourceProperties"])
    return {
        "PhysicalResourceId": "bill-bot-config-seed",
        "Data": {k: str(v) for k, v in counts.items()},
    }
