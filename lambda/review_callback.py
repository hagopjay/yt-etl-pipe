"""
review_callback.py — API Gateway (HTTP API) GET /review.

The other half of the security model documented in
request_human_review.py. Every check below exists to defend against a
SPECIFIC realistic failure mode of email-based approval links -- read
each comment before removing a check that looks redundant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("review_callback")

s3 = boto3.client("s3")
sfn = boto3.client("stepfunctions")
secrets = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

REVIEWS_TABLE = dynamodb.Table(os.environ["REVIEWS_TABLE"])
LANDING_BUCKET = os.environ["LANDING_BUCKET"]
HMAC_SECRET_ARN = os.environ["REVIEW_HMAC_SECRET_ARN"]

_cached_hmac_key = None


def _get_hmac_key() -> bytes:
    global _cached_hmac_key
    if _cached_hmac_key is None:
        _cached_hmac_key = secrets.get_secret_value(SecretId=HMAC_SECRET_ARN)["SecretString"].encode("utf-8")
    return _cached_hmac_key


def _sign(review_id: str, decision: str) -> str:
    key = _get_hmac_key()
    message = f"{review_id}:{decision}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _html(title: str, body: str) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": f"<html><head><title>{title}</title></head><body style='font-family:sans-serif;max-width:32rem;margin:4rem auto'>"
                f"<h2>{title}</h2><p>{body}</p></body></html>",
    }


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    review_id = params.get("review_id")
    decision = params.get("decision")
    signature = params.get("sig")

    if not review_id or decision not in ("approve", "reject") or not signature:
        return _html("Invalid link", "This review link is missing required parameters.")

    # Constant-time comparison -- a naive `==` here would leak signature
    # correctness one byte at a time via response-timing differences, a
    # textbook timing-attack surface for exactly this kind of
    # "does this signature match" check.
    expected_signature = _sign(review_id, decision)
    if not hmac.compare_digest(signature, expected_signature):
        log.warning("Signature mismatch for review_id=%s -- possible tampered link.", review_id)
        return _html("Invalid link", "This review link's signature does not match. It may have been altered.")

    # Single-use enforcement: only transition PENDING -> CONSUMED once.
    # A mail client's link-preview crawler pre-fetching this URL (a real,
    # common occurrence) will hit this condition on ITS request and get
    # a harmless "already handled" response; the human's own later click
    # then correctly reports "already handled" too rather than silently
    # doing nothing -- which is the honest behavior, even though it means
    # a sufficiently fast link-preview crawler can "eat" a real approval.
    # A production version would mitigate this with a confirmation page
    # (GET renders a button, POST performs the action) instead of a
    # single GET performing the action directly -- noted here rather than
    # implemented, to keep this project's frontend surface to the one
    # plain HTML form already in frontend/submit.html.
    try:
        item = REVIEWS_TABLE.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET #s = :consumed",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":consumed": "CONSUMED", ":pending": "PENDING"},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _html("Already handled", "This review has already been resolved (or has expired).")
        log.exception("Unexpected DynamoDB error consuming review_id=%s", review_id)
        return _html("Error", "Something went wrong recording your decision. Please contact an administrator.")

    task_token = item["task_token"]
    video_id = item["video_id"]

    if decision == "approve":
        staged = s3.get_object(Bucket=LANDING_BUCKET, Key=item["staging_s3_key"])
        payload = json.loads(staged["Body"].read())
        payload["reviewed_by_human"] = True
        sfn.send_task_success(taskToken=task_token, output=json.dumps(payload))
        log.info("Review approved for video_id=%s (review_id=%s)", video_id, review_id)
        return _html("Approved", f"Transcript for video <code>{video_id}</code> approved and will be published.")

    sfn.send_task_failure(taskToken=task_token, error="ReviewRejected", cause=f"Rejected by human reviewer (review_id={review_id})")
    log.info("Review rejected for video_id=%s (review_id=%s)", video_id, review_id)
    return _html("Rejected", f"Transcript for video <code>{video_id}</code> rejected and will not be published.")
