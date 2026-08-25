"""Shared test fixtures and helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parent.parent
LAMBDAS = REPO_ROOT / "lambdas"


def load_handler(lambda_name: str) -> ModuleType:
    """Import ``lambdas/<lambda_name>/handler.py`` under a unique module name.

    Every Lambda's entry point is called handler.py, so they would shadow each
    other on sys.path. Loading by explicit path keeps them distinct.
    """
    module_name = f"_handler_{lambda_name}"
    if module_name in sys.modules:
        return sys.modules[module_name]

    path = LAMBDAS / lambda_name / "handler.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def aws_credentials(monkeypatch):
    """Stop boto3 from finding real credentials during moto-backed tests."""
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-west-2",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def state_table(aws_credentials, monkeypatch):
    """A moto-backed DynamoDB table matching the real single-table schema."""
    from moto import mock_aws

    with mock_aws():
        import boto3

        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName="bill-bot-test",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("TABLE_NAME", "bill-bot-test")
        yield table
