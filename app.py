#!/usr/bin/env python3
"""CDK app entry point for bill-bot.

Reads ``config.yaml`` at the repo root (copy ``config.example.yaml`` to start).
Context flags override the YAML for one-off ops:

    cdk deploy -c dryRun=false
"""

from __future__ import annotations

import os
import sys

import aws_cdk as cdk

from infra.bill_bot_stack import BillBotStack
from infra.config import ConfigError, load_config

# Rough US SMS cost per segment, inclusive of carrier fees, used only to make
# the deploy-time warning concrete. Confirm current rates on AWS's pricing page.
_USD_PER_SEGMENT = 0.01


def report_segment_costs(config) -> None:
    """Print an SMS segment estimate per template, warning on expensive ones.

    SMS bills per 160-character segment and a Venmo link alone is ~90
    characters, so a chatty template quietly costs 2-3x per recipient. Better
    to see that at deploy time than on the invoice.
    """
    estimates = config.messages.segment_estimates()
    limit = config.messaging.max_segments_warn
    receivers = len(config.receivers)

    print("SMS segment estimates (sample values, per recipient):")
    width = max(len(name) for name in estimates)
    for name, segments in sorted(estimates.items()):
        cost = segments * _USD_PER_SEGMENT
        flag = "  <-- over max_segments_warn" if segments > limit else ""
        print(f"  {name:<{width}}  ~{segments} segment(s)  ~${cost:.2f}{flag}")

    worst = estimates.get("bill", 0)
    print(
        f"  A bill notification to all {receivers} receivers costs roughly "
        f"${worst * _USD_PER_SEGMENT * receivers:.2f}."
    )

    over = {n: s for n, s in estimates.items() if s > limit}
    if over:
        print(
            f"\nWARNING: {', '.join(sorted(over))} exceed "
            f"messaging.max_segments_warn={limit}. Shorten the template(s) in "
            "config.yaml or raise the threshold if the cost is acceptable.",
            file=sys.stderr,
        )


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        # A stack trace here is noise - the message already names the field.
        sys.exit(f"config error: {exc}")

    app = cdk.App()

    dry_run_override = app.node.try_get_context("dryRun")
    dry_run = (
        str(dry_run_override).lower() == "true"
        if dry_run_override is not None
        else config.behavior.dry_run
    )
    if not dry_run and not config.messaging.origination_number_id:
        sys.exit(
            "config error: -c dryRun=false needs messaging.origination_number_id "
            "set in config.yaml"
        )

    report_segment_costs(config)
    if dry_run:
        print("\ndry_run is ON - messages will be logged, not sent.")
    else:
        print(
            f"\ndry_run is OFF - texts will really send from "
            f"{config.messaging.origination_number_id}."
        )

    BillBotStack(
        app,
        config.stack_name,
        config=config,
        dry_run=dry_run,
        env=cdk.Environment(
            account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
            region=config.region or os.environ.get("CDK_DEFAULT_REGION"),
        ),
    )
    app.synth()


if __name__ == "__main__":
    main()
