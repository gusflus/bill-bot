"""CDK assertions on the synthesized state table and seeder."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.bill_bot_stack import BillBotStack
from infra.config import load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.yaml"


@pytest.fixture(scope="module")
def template() -> Template:
    config = load_config(EXAMPLE_CONFIG)
    app = cdk.App()
    stack = BillBotStack(
        app,
        config.stack_name,
        config=config,
        dry_run=config.behavior.dry_run,
        env=cdk.Environment(account="123456789012", region="us-west-2"),
    )
    return Template.from_stack(stack)


def test_exactly_one_table(template):
    template.resource_count_is("AWS::DynamoDB::Table", 1)


def test_table_uses_the_single_table_key_schema(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
        },
    )


def test_table_is_retained_on_stack_deletion(template):
    """The payment ledger must survive a cdk destroy."""
    template.has_resource(
        "AWS::DynamoDB::Table",
        {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"},
    )


def test_table_is_encrypted_and_recoverable(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "SSESpecification": {"SSEEnabled": True},
            "PointInTimeRecoverySpecification": {
                "PointInTimeRecoveryEnabled": True
            },
        },
    )


def test_seeder_custom_resource_carries_the_config(template):
    """Config travels as JSON so CloudFormation re-seeds when config.yaml changes."""
    resources = template.find_resources("Custom::AWS::CDK::CustomResource")
    if not resources:
        resources = {
            k: v
            for k, v in template.to_json()["Resources"].items()
            if k.startswith("StateSeedConfig")
        }
    assert resources, "expected a config-seeding custom resource"

    props = next(iter(resources.values()))["Properties"]
    assert "Alex" in props["Receivers"]
    assert "billpay.pge.com" in props["Senders"]
    assert '"share": 2' in props["Receivers"]


def test_seeder_lambda_is_scoped_to_the_table(template):
    """Least privilege: the seeder can reach the table and nothing else."""
    policies = template.find_resources("AWS::IAM::Policy")
    dynamo_actions = [
        statement
        for policy in policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if any(
            str(action).startswith("dynamodb:")
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        )
    ]
    assert dynamo_actions, "seeder should have DynamoDB permissions"
    for statement in dynamo_actions:
        assert statement["Resource"] != "*"


def test_seeder_runs_on_python_312(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"Runtime": "python3.12", "Handler": "handler.handler"}),
    )


def test_stack_outputs_the_table_name(template):
    outputs = template.to_json()["Outputs"]
    assert "TableName" in outputs
    assert outputs["ReceiverCount"]["Value"] == "3"
    assert outputs["SenderCount"]["Value"] == "4"
