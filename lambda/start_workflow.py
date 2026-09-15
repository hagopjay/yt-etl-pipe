"""
start_workflow.py — triggered by the SQS FIFO `submissions` queue (event
source mapping, small batch size).

INSIGHT -- why SQS between API Gateway and Step Functions at all, instead
    of starting the execution directly from submit_handler:
    Three independent reasons, not one:
    1. Backpressure: Step Functions' StartExecution API has an account-
       level TPS limit. A burst of submissions (e.g. someone pastes 200
       URLs from a spreadsheet in a loop) would start throttling
       StartExecution calls directly; SQS absorbs the burst and lets
       consumption happen at whatever rate is safe.
    2. A natural retry/DLQ boundary for the "start the workflow" step
       itself, decoupled from the workflow's OWN internal retry logic
       (see stepfunctions/transcript_pipeline.asl.json) -- if
       StartExecution itself fails (throttling, transient AWS error),
       SQS's redrive policy handles it without needing bespoke retry code
       here.
    3. FIFO's per-video_id MessageGroupId ordering (see 07_sqs_sns.tf)
       is what actually prevents two concurrent executions for the same
       video -- that guarantee only exists because this hop is SQS, not
       a direct synchronous call.

INSIGHT -- execution naming as a THIRD idempotency layer:
    Step Functions execution names must be unique per state machine for
    90 days. Naming the execution deterministically from video_id means
    that even if somehow the same SQS message were delivered twice (SQS
    is at-least-once even within a FIFO queue -- FIFO guarantees ORDER
    and exactly-once-ish delivery under normal operation, but AWS's own
    documentation is careful to say at-least-once is still possible under
    failure conditions), the second StartExecution call fails with
    ExecutionAlreadyExists instead of silently starting a duplicate run.
    Three independent layers (DynamoDB conditional put, FIFO dedup,
    deterministic execution name) sounds like overkill for a demo
    project -- it's actually the minimum you'd want in production,
    because each layer covers a failure mode the others don't.
"""

from __future__ import annotations

import json
import logging
import os
import time

import boto3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("start_workflow")

sfn = boto3.client("stepfunctions")
dynamodb = boto3.resource("dynamodb")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
JOBS_TABLE = dynamodb.Table(os.environ["JOBS_TABLE"])


def handler(event, context):
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            payload = json.loads(record["body"])
            video_id = payload["video_id"]

            execution_name = f"vid-{video_id}"  # deterministic -- see docstring

            resp = sfn.start_execution(
                stateMachineArn=STATE_MACHINE_ARN,
                name=execution_name,
                input=json.dumps({"video_id": video_id, "source_url": payload.get("source_url")}),
            )

            JOBS_TABLE.update_item(
                Key={"video_id": video_id},
                UpdateExpression="SET #s = :processing, execution_arn = :arn, updated_at = :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":processing": "PROCESSING",
                    ":arn": resp["executionArn"],
                    ":now": int(time.time()),
                },
            )

        except sfn.exceptions.ExecutionAlreadyExists:
            log.info("Execution already exists for this video -- treating as success (idempotent).")
        except Exception:
            log.exception("Failed to start workflow for message_id=%s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
