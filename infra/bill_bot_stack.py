"""CDK stack for bill-bot.

Deliberately thin: it composes the constructs in ``infra/constructs/`` and emits
the outputs an operator needs. Resource definitions live in their construct
files.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Stack
from constructs import Construct

from infra.config import DeployConfig
from infra.constructs.ingest_api import IngestApi, grant_sms_send_to_ingest
from infra.constructs.messaging import InboundMessaging, Reminders
from infra.constructs.state_table import StateTable


class BillBotStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: DeployConfig,
        dry_run: bool,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        state = StateTable(self, "State", config=config)

        ingest = IngestApi(
            self,
            "Ingest",
            table=state.table,
            config=config,
            dry_run=dry_run,
        )
        grant_sms_send_to_ingest(ingest, config.messaging.origination_number_id)

        InboundMessaging(
            self,
            "Inbound",
            table=state.table,
            config=config,
            dry_run=dry_run,
            secret_arn_source=ingest.secret,
        )

        if config.reminders.enabled:
            Reminders(
                self,
                "Reminders",
                table=state.table,
                config=config,
                dry_run=dry_run,
                secret_arn_source=ingest.secret,
            )

        CfnOutput(self, "TableName", value=state.table.table_name)
        CfnOutput(self, "DryRun", value=str(dry_run))
        CfnOutput(
            self,
            "ReceiverCount",
            value=str(len(config.receivers)),
        )
        CfnOutput(self, "SenderCount", value=str(len(config.senders)))
