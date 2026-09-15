"""
get_status.py — API Gateway (HTTP API) GET /submissions/{video_id}.

Deliberately trivial: a straight DynamoDB read. Kept as its own Lambda
rather than folded into submit_handler because it has a different trust
profile (read-only, no downstream side effects, safe to poll aggressively
from a frontend) and a different IAM permission set (GetItem only, no
PutItem/UpdateItem) -- least privilege is easier to keep honest when the
function boundary already matches the permission boundary.
"""

from __future__ import annotations

import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
JOBS_TABLE = dynamodb.Table(os.environ["JOBS_TABLE"])


def handler(event, context):
    video_id = (event.get("pathParameters") or {}).get("video_id")
    if not video_id:
        return {"statusCode": 400, "body": json.dumps({"error": "video_id path parameter required"})}

    item = JOBS_TABLE.get_item(Key={"video_id": video_id}).get("Item")
    if not item:
        return {"statusCode": 404, "body": json.dumps({"error": "unknown video_id"})}

    # Never leak the task_token field (a live Step Functions bearer
    # credential while a review is pending) into a public status response.
    item.pop("task_token", None)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(item, default=str),
    }
