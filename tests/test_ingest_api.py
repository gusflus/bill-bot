"""CDK assertions for the ingest API.

Weighted toward the security mitigations, because the Function URL is publicly
reachable and these are the controls standing in front of it. A refactor that
quietly drops reserved concurrency or widens the Bedrock policy should fail here.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.bill_bot_stack import BillBotStack
from infra.config import load_config


EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.yaml"


def build(dry_run: bool = True, origination_number_id: str = "",
          reminders: bool = False) -> Template:
    config = load_config(EXAMPLE_CONFIG)
    if origination_number_id:
        config = _with_number(config, origination_number_id)
    if reminders:
        config = _with_reminders(config)
    app = cdk.App()
    stack = BillBotStack(
        app,
        config.stack_name,
        config=config,
        dry_run=dry_run,
        env=cdk.Environment(account="123456789012", region="us-west-2"),
    )
    return Template.from_stack(stack)


def _with_number(config, number_id):
    import dataclasses

    return dataclasses.replace(
        config,
        messaging=dataclasses.replace(
            config.messaging, origination_number_id=number_id
        ),
    )


def _with_reminders(config):
    import dataclasses

    return dataclasses.replace(
        config, reminders=dataclasses.replace(config.reminders, enabled=True)
    )


@pytest.fixture(scope="module")
def template() -> Template:
    return build()


def _function_by_prefix(template: Template, prefix: str) -> dict:
    functions = template.find_resources("AWS::Lambda::Function")
    matches = [v for k, v in functions.items() if k.startswith(prefix)]
    assert len(matches) == 1, f"expected one {prefix} function, got {len(matches)}"
    return matches[0]


def ingest_function(template: Template) -> dict:
    return _function_by_prefix(template, "IngestFn")


def inbound_function(template: Template) -> dict:
    return _function_by_prefix(template, "InboundFn")


# --- the public surface -------------------------------------------------------


def test_function_url_exists(template):
    template.resource_count_is("AWS::Lambda::Url", 1)


def test_function_url_is_unauthenticated_by_design(template):
    """Auth is the shared secret header, checked in the handler."""
    template.has_resource_properties("AWS::Lambda::Url", {"AuthType": "NONE"})




# --- the shared secret --------------------------------------------------------


def test_secret_is_generated_not_hardcoded(template):
    template.resource_count_is("AWS::SecretsManager::Secret", 1)
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    props = next(iter(secrets.values()))["Properties"]
    assert "GenerateSecretString" in props
    assert "SecretString" not in props, "secret value must never be in the template"


def test_secret_is_header_safe(template):
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    gen = next(iter(secrets.values()))["Properties"]["GenerateSecretString"]
    assert gen["PasswordLength"] == 48
    assert gen["ExcludePunctuation"] is True


def test_function_receives_the_secret_arn_not_the_value(template):
    env = ingest_function(template)["Properties"]["Environment"]["Variables"]
    assert "SECRET_ARN" in env
    assert isinstance(env["SECRET_ARN"], dict)  # a CloudFormation Ref


# --- IAM scoping --------------------------------------------------------------


def _statements(template: Template) -> list[dict]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _actions(statement: dict) -> list[str]:
    action = statement["Action"]
    return action if isinstance(action, list) else [action]


def test_bedrock_is_scoped_to_the_configured_model(template):
    bedrock = [
        s for s in _statements(template)
        if any(a.startswith("bedrock:") for a in _actions(s))
    ]
    assert bedrock, "expected a Bedrock policy"
    for statement in bedrock:
        assert _actions(statement) == ["bedrock:InvokeModel"]
        resources = statement["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        assert "*" not in resources
        assert any("claude-haiku-4-5" in str(r) for r in resources)


def test_no_policy_grants_star_on_star(template):
    for statement in _statements(template):
        if statement.get("Effect") != "Allow":
            continue
        if statement["Resource"] == "*":
            # Only broad-by-necessity actions may use *; nothing data-plane.
            for action in _actions(statement):
                assert action.startswith(
                    ("logs:", "xray:", "cloudwatch:")
                ), f"{action} should not be granted on *"


# --- runtime environment ------------------------------------------------------


def test_dry_run_is_propagated(template):
    env = ingest_function(template)["Properties"]["Environment"]["Variables"]
    assert env["DRY_RUN"] == "true"


def test_dry_run_false_is_propagated():
    template = build(dry_run=False, origination_number_id="phone-abc123")
    env = ingest_function(template)["Properties"]["Environment"]["Variables"]
    assert env["DRY_RUN"] == "false"
    assert env["ORIGINATION_NUMBER_ID"] == "phone-abc123"


def test_no_lambda_reserves_concurrency(template):
    """Plain Lambdas - no reservations to trip over account concurrency limits."""
    for fn in template.find_resources("AWS::Lambda::Function").values():
        assert "ReservedConcurrentExecutions" not in fn["Properties"]


def test_message_templates_travel_as_json(template):
    env = ingest_function(template)["Properties"]["Environment"]["Variables"]
    messages = json.loads(env["MESSAGES"])
    assert set(messages) == {
        "bill",
        "paid_ack",
        "status",
        "reminder",
        "payment_alert",
        "venmo_note",
    }
    assert "{venmo_link}" in messages["bill"]
    assert "{biller}" in messages["venmo_note"]


def test_payer_details_are_propagated(template):
    env = ingest_function(template)["Properties"]["Environment"]["Variables"]
    assert env["PAYER_LABEL"] == "Gus"
    assert env["PAYER_PHONE"] == "+15551230000"
    assert env["PAYER_SHARE"] == "1"
    assert env["NOTIFY_ON_PAYMENT"] == "true"


def test_payee_and_timezone_are_propagated(template):
    env = ingest_function(template)["Properties"]["Environment"]["Variables"]
    assert env["VENMO_USERNAME"] == "your-venmo-handle"
    assert env["ZELLE_CONTACT"] == "you@example.com"
    assert env["TIMEZONE"] == "America/Los_Angeles"


def test_receivers_are_not_in_the_environment(template):
    """They live in DynamoDB - that's what makes them editable without a redeploy.

    The payer's own number is here, because it's a single deploy-time constant
    rather than a list the Lambda iterates.
    """
    env = ingest_function(template)["Properties"]["Environment"]["Variables"]
    rendered = str(sorted(env.items(), key=lambda kv: kv[0]))
    for number in ("+15551230002", "+15551230003", "+15551230004"):
        assert number not in rendered


# --- shared code layer --------------------------------------------------------


def test_shared_modules_ship_as_one_layer(template):
    template.resource_count_is("AWS::Lambda::LayerVersion", 1)
    assert ingest_function(template)["Properties"]["Layers"]


def test_layer_asset_puts_modules_under_python():
    """Regression guard for a bug the other tests can't see.

    A Python layer only reaches sys.path when its content sits under python/,
    arriving at /opt/python. Flatten it and every Lambda import fails at runtime
    while the unit tests keep passing, because they read the files off disk.
    """
    shared = Path(__file__).parent.parent / "lambdas" / "shared"
    modules = shared / "python"

    assert modules.is_dir(), "layer asset must contain a python/ directory"
    assert (modules / "shares.py").exists()
    assert (modules / "extract.py").exists()
    # Nothing importable may sit at the asset root, or it silently won't load.
    assert not list(shared.glob("*.py"))


def test_every_lambda_gets_the_shared_layer(template):
    """Each handler imports from lambdas/shared, so each needs the layer."""
    for prefix in ("IngestFn", "InboundFn"):
        assert _function_by_prefix(template, prefix)["Properties"]["Layers"]


# --- outputs ------------------------------------------------------------------


def test_outputs_tell_the_operator_what_to_configure(template):
    outputs = template.to_json()["Outputs"]
    names = {k for k in outputs}
    assert any("IngestUrl" in n for n in names)
    assert any("SharedSecretArn" in n for n in names)


# --- inbound messaging --------------------------------------------------------


def test_inbound_topic_exists_for_two_way_sms(template):
    template.resource_count_is("AWS::SNS::Topic", 1)


def test_end_user_messaging_may_publish_to_the_topic(template):
    policies = template.find_resources("AWS::SNS::TopicPolicy")
    statements = [
        s
        for p in policies.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    principals = [
        s.get("Principal", {}).get("Service") for s in statements
    ]
    assert "sms-voice.amazonaws.com" in principals


def test_inbound_lambda_subscribes_to_the_topic(template):
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "lambda"}
    )


def test_inbound_topic_arn_is_an_output(template):
    """The operator wires the phone number's two-way config to this by hand."""
    outputs = template.to_json()["Outputs"]
    assert any("InboundTopicArn" in n for n in outputs)


def test_no_phone_number_resource_is_created(template):
    """A registered toll-free number must never be CloudFormation-managed."""
    resources = template.to_json()["Resources"]
    types = {r["Type"] for r in resources.values()}
    assert not any("PhoneNumber" in t for t in types)


def test_sms_send_is_not_granted_without_a_configured_number(template):
    """dry-run development shouldn't carry permission it can't use."""
    sms = [
        s for s in _statements(template)
        if any(a.startswith("sms-voice:") for a in _actions(s))
    ]
    assert sms == []


def test_sms_send_is_scoped_to_the_number_when_configured():
    template = build(dry_run=False, origination_number_id="phone-abc123")
    sms = [
        s for s in _statements(template)
        if any(a.startswith("sms-voice:") for a in _actions(s))
    ]
    assert sms, "expected SendTextMessage permission once a number is configured"
    for statement in sms:
        assert _actions(statement) == ["sms-voice:SendTextMessage"]
        assert "phone-abc123" in str(statement["Resource"])
    # Both the bill sender and the reply sender need it.
    assert len(sms) == 2


# --- reminders ----------------------------------------------------------------


def test_no_reminder_schedule_when_disabled(template):
    """Default config leaves reminders off, so nothing should be scheduled."""
    template.resource_count_is("AWS::Events::Rule", 0)


def test_reminder_schedule_created_when_enabled():
    template = build(reminders=True)
    template.resource_count_is("AWS::Events::Rule", 1)
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "cron(0 17 * * ? *)"}
    )


def test_reminder_function_gets_its_thresholds():
    template = build(reminders=True)
    env = _function_by_prefix(template, "RemindersFn")["Properties"]["Environment"][
        "Variables"
    ]
    assert env["REMINDER_AFTER_DAYS"] == "3"
    assert env["REMINDER_REPEAT_DAYS"] == "3"
