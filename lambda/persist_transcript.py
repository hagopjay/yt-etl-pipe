"""
persist_transcript.py — Step Functions Task, the terminal success step
for every path through the pipeline (direct caption fetch, auto-approved
high-confidence ASR, or human-approved low-confidence ASR).

INSIGHT -- this is where the new subsystem REJOINS the base project's
    architecture, not a new architecture of its own:
    The write target is `landing/transcripts/...` in the SAME landing
    bucket the base pipeline already crawls (terraform/04_glue.tf's
    `transcripts` crawler) and the SAME Glue Catalog database. From this
    point forward, a transcript is just another row a Glue Spark job or
    an Athena query can join against `video_statistics` on `video_id` --
    "what do the highest-confidence transcripts of the most-commented
    videos in the Gaming category say" becomes a single Athena JOIN, not
    a cross-system integration project.

INSIGHT -- partitioning by ingestion date, not video publish date:
    Unlike the base project's video-statistics data (which has a
    meaningful, analysis-relevant publish date), a transcript's relevant
    "when" for partition-pruning purposes is when a human requested it,
    not when the underlying video went up -- queries like "what did we
    transcribe this week" are far more likely against this table than
    "what did we transcribe from videos published this week". Partition
    key choice should follow the query pattern the table actually serves,
    which differs table-to-table even within one pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("persist_transcript")

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

LANDING_BUCKET = os.environ["LANDING_BUCKET"]
JOBS_TABLE = dynamodb.Table(os.environ["JOBS_TABLE"])


def handler(event, context):
    video_id = event["video_id"]
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    record = {
        "video_id": video_id,
        "language": event.get("language"),
        "transcript_source": event.get("transcript_source"),
        "is_generated": event.get("is_generated"),
        "asr_avg_confidence": event.get("asr_avg_confidence"),
        "reviewed_by_human": event.get("reviewed_by_human", False),
        "segments": event.get("segments", []),
        "full_text": " ".join(s["text"] for s in event.get("segments", [])),
        "persisted_at": datetime.now(timezone.utc).isoformat(),
    }

    key = f"transcripts/dt={dt}/video_id={video_id}/transcript.json"
    s3.put_object(
        Bucket=LANDING_BUCKET,
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
        ContentType="application/json",
    )

    JOBS_TABLE.update_item(
        Key={"video_id": video_id},
        UpdateExpression=(
            "SET #s = :completed, s3_path = :path, transcript_source = :src, "
            "updated_at = :now REMOVE task_token"  # scrub the (now-consumed) task token -- it should never linger past its useful life
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":completed": "COMPLETED",
            ":path": f"s3://{LANDING_BUCKET}/{key}",
            ":src": record["transcript_source"],
            ":now": int(time.time()),
        },
    )

    log.info("Persisted transcript for video_id=%s -> s3://%s/%s", video_id, LANDING_BUCKET, key)
    return {"status": "COMPLETED", "video_id": video_id, "s3_path": f"s3://{LANDING_BUCKET}/{key}"}
