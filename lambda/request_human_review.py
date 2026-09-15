"""
request_human_review.py — Step Functions Task, `.waitForTaskToken`
integration. Reached only when evaluate_confidence.py flagged a
low-confidence ASR transcript.

================================================================================
SECURITY MODEL FOR THE APPROVE/REJECT EMAIL LINKS (read before changing)
================================================================================
The Step Functions task token is a bearer credential: whoever presents
it can resume (or fail) this specific paused execution. It must NEVER be
placed directly in an emailed URL -- email is not a secure channel
(forwarding, shared inboxes, mail-client link-preview crawlers that
GET every URL in a message automatically), so anything in that URL
should be treated as eventually-public.

The design here uses three layers instead:
  1. An OPAQUE review_id (a random UUID, not the video_id or anything
     else guessable) is what actually appears in the email link. The
     real task token lives only in DynamoDB, addressed by review_id.
  2. An HMAC signature over (review_id, decision), keyed by a secret
     Terraform generates and stores in Secrets Manager, is ALSO in the
     URL. This stops someone from taking a legitimate "reject" link and
     editing the query string to "approve" -- the signature would no
     longer match.
  3. Single-use enforcement via a CONDITIONAL DynamoDB update in
     review_callback.py: the first request to consume a review_id wins;
     a mail-client link-preview crawler firing the GET a second time (a
     real, common failure mode for email-based approval flows) gets a
     "already handled" response instead of accidentally reversing a
     human's decision or double-firing send_task_success.
  4. A DynamoDB TTL (`review_token_ttl_hours`, default 72h) expires
     stale reviews automatically -- an execution that's waited 3 days
     with the video's context growing stale should time out into a safe
     default (this project sends it to REJECTED / needs-attention on Step
     Functions' own state timeout, configured in the ASL) rather than
     stay resumable indefinitely.
================================================================================
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid

import boto3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("request_human_review")

s3 = boto3.client("s3")
sns = boto3.client("sns")
secrets = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

REVIEWS_TABLE = dynamodb.Table(os.environ["REVIEWS_TABLE"])
LANDING_BUCKET = os.environ["LANDING_BUCKET"]
REVIEW_TOPIC_ARN = os.environ["REVIEW_TOPIC_ARN"]
HMAC_SECRET_ARN = os.environ["REVIEW_HMAC_SECRET_ARN"]
API_BASE_URL = os.environ["API_BASE_URL"]
TTL_HOURS = int(os.environ.get("REVIEW_TOKEN_TTL_HOURS", "72"))

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


def handler(event, context):
    video_id = event["video_id"]
    task_token = event["taskToken"]

    review_id = str(uuid.uuid4())

    # The full transcript payload can be large (a long video's segment
    # list) -- DynamoDB items are capped at 400KB, and stuffing a large
    # transcript into the review row risks hitting that ceiling on
    # exactly the videos where a human review is most likely (long,
    # rambly content that also confused the ASR). Stage it in S3 instead
    # and store only the pointer in DynamoDB.
    staging_key = f"pending_review/{review_id}.json"
    s3.put_object(
        Bucket=LANDING_BUCKET,
        Key=staging_key,
        Body=json.dumps(event).encode("utf-8"),
        ContentType="application/json",
    )

    now = int(time.time())
    REVIEWS_TABLE.put_item(Item={
        "review_id": review_id,
        "video_id": video_id,
        "task_token": task_token,
        "staging_s3_key": staging_key,
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + TTL_HOURS * 3600,  # DynamoDB TTL attribute
    })

    approve_sig = _sign(review_id, "approve")
    reject_sig = _sign(review_id, "reject")
    approve_url = f"{API_BASE_URL}/review?review_id={review_id}&decision=approve&sig={approve_sig}"
    reject_url = f"{API_BASE_URL}/review?review_id={review_id}&decision=reject&sig={reject_sig}"

    sns.publish(
        TopicArn=REVIEW_TOPIC_ARN,
        Subject=f"[Review needed] Low-confidence transcript — {video_id}",
        Message=(
            f"Video {video_id} was transcribed via ASR fallback with average "
            f"confidence {event.get('asr_avg_confidence', 'unknown')}, below the "
            f"auto-publish threshold.\n\n"
            f"Approve (publish as-is): {approve_url}\n"
            f"Reject (discard): {reject_url}\n\n"
            f"This link expires in {TTL_HOURS} hours and can only be used once."
        ),
    )

    log.info("Requested human review review_id=%s for video_id=%s", review_id, video_id)
    # No return value needed -- execution is now paused on task_token
    # until review_callback.py resolves it.
    return {"review_id": review_id}
