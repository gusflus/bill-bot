"""Inbound SMS: the SNS topic and the Lambda that answers replies.

The toll-free number itself is deliberately *not* a CloudFormation resource.
Getting one verified takes real time, and a stack update that replaced or
released it would mean starting registration over. So the number is provisioned
by hand and referenced by ID from config.yaml.

The consequence is one manual wiring step: this construct creates the SNS topic,
and you point the number's two-way SMS configuration at it once. The topic ARN is
a stack output for exactly that purpose.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

from infra.config import DeployConfig
from infra.constructs.ingest_api import (
    ASSET_EXCLUDE,
    runtime_environment,
    shared_layer,
)


class InboundMessaging(Construct):
    """SNS topic for inbound SMS plus the handler that replies to it."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        config: DeployConfig,
        dry_run: bool,
        secret_arn_source,
    ) -> None:
        super().__init__(scope, construct_id)

        self.topic = sns.Topic(
            self,
            "InboundTopic",
            display_name="bill-bot inbound SMS",
        )
        # AWS End User Messaging publishes here on behalf of the phone number.
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                principals=[iam.ServicePrincipal("sms-voice.amazonaws.com")],
                resources=[self.topic.topic_arn],
            )
        )

        log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.function = lambda_.Function(
            self,
            "Fn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/inbound", exclude=ASSET_EXCLUDE),
            timeout=Duration.seconds(30),
            memory_size=256,
            log_group=log_group,
            environment=runtime_environment(
                table, config, dry_run, secret_arn_source
            ),
        )
        self.function.add_layers(shared_layer(self, table.stack))
        self.function.add_event_source(sources.SnsEventSource(self.topic))

        table.grant_read_write_data(self.function)
        grant_sms_send(self.function, config.messaging.origination_number_id)

        CfnOutput(
            self,
            "InboundTopicArn",
            value=self.topic.topic_arn,
            description=(
                "Point your toll-free number's two-way SMS configuration at "
                "this topic so PAID and STATUS replies reach the bot"
            ),
        )


class Reminders(Construct):
    """Scheduled nudges for roommates who still owe on a bill."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        config: DeployConfig,
        dry_run: bool,
        secret_arn_source,
    ) -> None:
        super().__init__(scope, construct_id)

        log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        environment = dict(
            runtime_environment(table, config, dry_run, secret_arn_source)
        )
        environment["REMINDER_AFTER_DAYS"] = str(config.reminders.after_days)
        environment["REMINDER_REPEAT_DAYS"] = str(config.reminders.repeat_days)

        self.function = lambda_.Function(
            self,
            "Fn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/reminders", exclude=ASSET_EXCLUDE),
            timeout=Duration.seconds(120),
            memory_size=256,
            log_group=log_group,
            environment=environment,
        )
        self.function.add_layers(shared_layer(self, table.stack))
        table.grant_read_write_data(self.function)
        grant_sms_send(self.function, config.messaging.origination_number_id)

        # Once a day. Nudging more often than that would be nagging, and the
        # repeat_days check in the handler is the real guard anyway.
        events.Rule(
            self,
            "Schedule",
            schedule=events.Schedule.cron(
                minute="0", hour=str(config.reminders.hour_utc)
            ),
            targets=[targets.LambdaFunction(self.function)],
            description="bill-bot unpaid reminders",
        )


def grant_sms_send(fn: lambda_.Function, origination_number_id: str) -> None:
    """Allow sending only from the configured number, where one is configured.

    With no number yet (dry-run development), grant nothing - the code path that
    would need it is unreachable, and a wildcard here would be permission we
    never audit again.
    """
    if not origination_number_id:
        return
    stack = fn.stack
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["sms-voice:SendTextMessage"],
            resources=[
                f"arn:aws:sms-voice:{stack.region}:{stack.account}:"
                f"phone-number/{origination_number_id}"
            ],
        )
    )
