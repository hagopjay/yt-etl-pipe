"""
transcribe_callback.py — triggered by an EventBridge rule matching AWS
Transcribe's own "Transcribe Job State Change" event (source:
aws.transcribe), filtered to COMPLETED and FAILED (see
terraform/12_eventbridge.tf).

This is the other half of the waitForTaskToken pattern started in
start_transcribe_job.py: it recovers which video/execution this job
belongs to, reads the actual transcript + confidence scores out of the
Transcribe output JSON, and resumes the paused Step Functions execution.

INSIGHT -- recovering video_id from the job name, not a lookup table:
    Transcribe's EventBridge event payload includes TranscriptionJobName
    but not arbitrary custom metadata. Rather than maintaining a second
    job-name -> video_id index, the job name is CONSTRUCTED to be
    self-describing (`transcript-{video_id}-{8hexchars}`, see
    start_transcribe_job.py) and parsed back apart here. Splitting from
    the RIGHT on the last hyphen is what makes this safe even though
    YouTube video IDs themselves may legally contain hyphens: the 8-hex-
    character suffix never contains one, so `rsplit("-", 1)` always
    isolates exactly the suffix, regardless of the video_id's own content.
"""

from __future__ import annotations

import json
import logging
import os
import statistics

import boto3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("transcribe_callback")

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")
sfn = boto3.client("stepfunctions")
dynamodb = boto3.resource("dynamodb")

LANDING_BUCKET = os.environ["LANDING_BUCKET"]
JOBS_TABLE = dynamodb.Table(os.environ["JOBS_TABLE"])
JOB_NAME_PREFIX = "transcript"


def _video_id_from_job_name(job_name: str) -> str:
    without_prefix = job_name[len(JOB_NAME_PREFIX) + 1:]  # strip "transcript-"
    video_id, _suffix = without_prefix.rsplit("-", 1)
    return video_id


def handler(event, context):
    detail = event["detail"]
    job_name = detail["TranscriptionJobName"]
    status = detail["TranscriptionJobStatus"]  # "COMPLETED" or "FAILED"
    video_id = _video_id_from_job_name(job_name)

    item = JOBS_TABLE.get_item(Key={"video_id": video_id}).get("Item")
    if not item or "task_token" not in item:
        log.error("No pending task token found for video_id=%s (job=%s) -- cannot resume execution.", video_id, job_name)
        return

    task_token = item["task_token"]

    if status == "FAILED":
        job_detail = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        reason = job_detail["TranscriptionJob"].get("FailureReason", "unknown")
        log.warning("Transcribe job %s failed: %s", job_name, reason)
        sfn.send_task_failure(taskToken=task_token, error="TranscribeJobFailed", cause=reason)
        return

    result_key = f"transcripts_raw_asr/{video_id}/{job_name}.json"
    obj = s3.get_object(Bucket=LANDING_BUCKET, Key=result_key)
    transcribe_output = json.loads(obj["Body"].read())

    segments, confidences = _parse_transcribe_output(transcribe_output)
    avg_confidence = round(statistics.mean(confidences), 4) if confidences else 0.0

    result = {
        "status": "SUCCESS",
        "video_id": video_id,
        "language": transcribe_output.get("results", {}).get("language_code", "unknown"),
        "is_generated": True,
        "segments": segments,
        "transcript_source": "asr",
        "asr_avg_confidence": avg_confidence,
    }

    sfn.send_task_success(taskToken=task_token, output=json.dumps(result))
    log.info("Resumed execution for video_id=%s with avg_confidence=%.3f", video_id, avg_confidence)


def _parse_transcribe_output(output: dict) -> tuple[list[dict], list[float]]:
    """Transcribe's batch output gives per-ITEM (roughly per-word)
    timing and confidence, not per-sentence segments the way YouTube's
    caption format does. Grouping into ~10-second buckets keeps the
    output SHAPE-COMPATIBLE with the caption-fetch path's `segments`
    list (start/duration/text), which is what lets persist_transcript.py
    and everything downstream (Glue, Athena) treat caption-sourced and
    ASR-sourced transcripts identically, distinguished only by the
    `transcript_source` / `asr_avg_confidence` fields."""
    items = output["results"]["items"]
    confidences = [
        float(i["alternatives"][0]["confidence"])
        for i in items
        if i["type"] == "pronunciation" and i["alternatives"][0].get("confidence") is not None
    ]

    segments = []
    bucket_start = None
    bucket_words: list[str] = []
    BUCKET_SECONDS = 10.0

    for item in items:
        if item["type"] == "punctuation":
            if bucket_words:
                bucket_words[-1] += item["alternatives"][0]["content"]
            continue

        start_time = float(item["start_time"])
        if bucket_start is None:
            bucket_start = start_time
        if start_time - bucket_start >= BUCKET_SECONDS and bucket_words:
            segments.append({
                "start": bucket_start,
                "duration": round(start_time - bucket_start, 2),
                "text": " ".join(bucket_words),
            })
            bucket_start = start_time
            bucket_words = []

        bucket_words.append(item["alternatives"][0]["content"])

    if bucket_words:
        segments.append({
            "start": bucket_start or 0.0,
            "duration": 0.0,
            "text": " ".join(bucket_words),
        })

    return segments, confidences
