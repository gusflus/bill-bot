"""The single DynamoDB table, plus the custom resource that seeds config into it.

One table holds four kinds of row, distinguished by key prefix:

    PK                          SK                  what it is
    CONFIG                      RECEIVER#<e164>     a roommate + their share
    CONFIG                      SENDER#<address>    a bill sender to watch
    BILL#<provider>#<YYYY-MM>   META                one bill
    BILL#<provider>#<YYYY-MM>   PAY#<e164>          one roommate's ledger row

CONFIG rows are owned by ``cdk deploy`` - seeded and pruned from config.yaml on
every deploy. BILL rows are runtime data and deploys never touch them.

Removal policy is RETAIN: a `cdk destroy` should not take the payment ledger
with it.
"""

from __future__ import annotations

import json

from aws_cdk import CustomResource, Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

from infra.config import DeployConfig
from infra.constructs.ingest_api import ASSET_EXCLUDE


class StateTable(Construct):
    """The bill-bot state table and its config seeder."""

    def __init__(
        self, scope: Construct, construct_id: str, *, config: DeployConfig
    ) -> None:
        super().__init__(scope, construct_id)

        self.table = dynamodb.Table(
            self,
            "Table",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            # A household generates a few dozen writes a month; provisioned
            # capacity would cost more than the data is worth.
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Bill rows carry a TTL because bill emails contain account numbers
            # and there's no reason to keep them forever.
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self._add_seeder(config)

    def _add_seeder(self, config: DeployConfig) -> None:
        seed_log_group = logs.LogGroup(
            self,
            "SeedFnLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        seed_fn = lambda_.Function(
            self,
            "SeedFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/seed_config", exclude=ASSET_EXCLUDE),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={"TABLE_NAME": self.table.table_name},
            log_group=seed_log_group,
        )
        self.table.grant_read_write_data(seed_fn)

        provider = cr.Provider(
            self,
            "SeedProvider",
            on_event_handler=seed_fn,
        )

        # Passing the config as JSON means CloudFormation sees a property diff
        # whenever config.yaml changes, which is what triggers a re-seed.
        CustomResource(
            self,
            "SeedConfig",
            service_token=provider.service_token,
            properties={
                "Receivers": json.dumps(
                    [
                        {"label": r.label, "phone": r.phone, "share": r.share}
                        for r in config.receivers
                    ]
                ),
                "Senders": json.dumps(
                    [
                        {
                            "id": s.id,
                            "name": s.name,
                            "from_address": s.from_address,
                        }
                        for s in config.senders
                    ]
                ),
            },
        )
