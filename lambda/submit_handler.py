"""
submit_handler.py — API Gateway (HTTP API) POST /submissions.

The human-in-the-loop entry point: a person pastes a YouTube URL into
the frontend form (frontend/submit.html), it hits this Lambda, and this
Lambda is the ONLY place in the whole subsystem that makes the
"should we even attempt this video" decision before committing resources.

INSIGHT -- idempotency is enforced HERE, not downstream:
    A conditional DynamoDB put means a second submission of a video
    that's already QUEUED/PROCESSING/AWAITING_REVIEW is rejected before
    it ever reaches SQS, before a Step Functions execution starts, before
    a proxy-fetch attempt burns rate-limit budget. A video that already
    COMPLETED returns the cached result instantly. This is cheaper and
    simpler than deduplicating later in the pipeline (SQS FIFO content-
    based dedup is a second, coarser layer of the same protection --
    it only catches EXACT duplicate message bodies within a 5-minute
    window, not "this video finished processing three days ago").
"""

from __future__ import annotations

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

from youtube_url import extract_video_id

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("submit_handler")

dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")

JOBS_TABLE = dynamodb.Table(os.environ["JOBS_TABLE"])
QUEUE_URL = os.environ["QUEUE_URL"]

TERMINAL_STATES = {"COMPLETED", "NO_CAPTIONS", "REJECTED"}
IN_FLIGHT_STATES = {"QUEUED", "PROCESSING", "AWAITING_REVIEW", "BLOCKED_RETRYING"}


def _response(status: int, body: dict):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body must be valid JSON."})

    raw_url = (body.get("url") or "").strip()
    if not raw_url:
        return _response(400, {"error": "Field 'url' is required."})

    video_id = extract_video_id(raw_url)
    if not video_id:
        return _response(400, {
            "error": "Could not recognize a YouTube video ID in the provided URL.",
            "hint": "Expected a youtube.com/watch, youtu.be, or /shorts/ link.",
        })

    now = int(time.time())

    try:
        JOBS_TABLE.put_item(
            Item={
                "video_id": video_id,
                "status": "QUEUED",
                "source_url": raw_url,
                "submitted_at": now,
                "updated_at": now,
            },
            # Only create a new row if one doesn't already exist. This is
            # the entire idempotency mechanism for first-time submissions.
            ConditionExpression="attribute_not_exists(video_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            log.exception("Unexpected DynamoDB error for video_id=%s", video_id)
            return _response(500, {"error": "Internal error recording submission."})

        # Row already exists -- decide what to tell the caller based on
        # its current status, without touching SQS/Step Functions again.
        existing = JOBS_TABLE.get_item(Key={"video_id": video_id}).get("Item", {})
        status = existing.get("status", "UNKNOWN")

        if status == "COMPLETED":
            return _response(200, {
                "video_id": video_id,
                "status": status,
                "s3_path": existing.get("s3_path"),
                "transcript_source": existing.get("transcript_source"),
                "message": "Already processed -- returning cached result.",
            })
        if status in IN_FLIGHT_STATES:
            return _response(202, {
                "video_id": video_id,
                "status": status,
                "message": "Already in the pipeline -- no new work started.",
            })
        if status in ("NO_CAPTIONS", "REJECTED"):
            # Deliberately allow re-submission of a terminal-but-not-
            # successful state: NO_CAPTIONS could change if the creator
            # later adds captions, and REJECTED could reflect a reviewer
            # decision worth revisiting. Reset to QUEUED and proceed.
            JOBS_TABLE.update_item(
                Key={"video_id": video_id},
                UpdateExpression="SET #s = :queued, updated_at = :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":queued": "QUEUED", ":now": now},
            )
        else:
            return _response(202, {"video_id": video_id, "status": status,
                                    "message": "Already known to the system."})

    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps({"video_id": video_id, "source_url": raw_url}),
        MessageGroupId=video_id,  # FIFO ordering/serialization scope -- see terraform/07_sqs_sns.tf
    )

    return _response(202, {
        "video_id": video_id,
        "status": "QUEUED",
        "message": "Submitted. Poll GET /submissions/{video_id} for status.",
    })
