"""
start_transcribe_job.py — Step Functions Task, `.waitForTaskToken`
integration pattern (see stepfunctions/transcript_pipeline.asl.json).

Runs only when fetch_captions returned NO_CAPTIONS (or DISABLED, if
`respect_creator_disabled_captions=false`) AND `enable_asr_fallback=true`.

DEPLOYMENT NOTE: this function needs the `yt-dlp` Python package AND a
static `ffmpeg` binary on PATH to extract audio -- that combination does
not fit comfortably in a standard Lambda zip + layer (ffmpeg static
builds run 70-80MB). Deploy this ONE function as a container-image Lambda
(a Dockerfile based on a Python slim image with ffmpeg installed) rather
than a zip -- every other function in this project is a plain zip
deployment; this is the deliberate exception, and the extra packaging
complexity is exactly why the ASR path is feature-flagged off by default
in favor of the far cheaper/simpler caption-fetch path.

INSIGHT -- why `.waitForTaskToken` instead of polling for Transcribe
    completion inside this function:
    A Transcribe batch job can take anywhere from under a minute to
    several minutes depending on audio length. Blocking a Lambda
    invocation on a poll loop for that long burns Lambda duration-based
    billing for pure idle waiting and risks hitting the function timeout
    on longer videos. The waitForTaskToken pattern instead returns
    immediately after START-ing the job, and a SEPARATE, event-driven
    path (transcribe_callback.py, triggered by EventBridge on the
    Transcribe job's own state-change event) resumes the Step Functions
    execution when the job actually finishes -- zero compute spent
    waiting, and no timeout risk regardless of audio length.

INSIGHT -- why the task token is stored on the JOBS table instead of a
    dedicated side table:
    Unlike the human-review task token (see request_human_review.py),
    this token's lifetime and access pattern map 1:1 onto the video's
    own job record -- there's exactly one in-flight ASR wait per video,
    keyed by the same video_id the jobs table already uses as its
    partition key. Introducing a second table here would be structure
    for its own sake; the human-review case genuinely needs different
    security properties (see that file's docstring) that justify the
    separate table there.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid

import boto3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("start_transcribe_job")

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")
dynamodb = boto3.resource("dynamodb")

AUDIO_BUCKET = os.environ["AUDIO_STAGING_BUCKET"]
LANDING_BUCKET = os.environ["LANDING_BUCKET"]
JOBS_TABLE = dynamodb.Table(os.environ["JOBS_TABLE"])
JOB_NAME_PREFIX = "transcript"


def handler(event, context):
    video_id = event["video_id"]
    task_token = event["taskToken"]  # injected by the waitForTaskToken integration

    # Persist the token BEFORE kicking off any long-running work, so a
    # crash between "download audio" and "start Transcribe job" still
    # leaves a recoverable trail rather than an orphaned execution with
    # no way to resume it.
    JOBS_TABLE.update_item(
        Key={"video_id": video_id},
        UpdateExpression="SET task_token = :tok, #s = :status, updated_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":tok": task_token,
            ":status": "TRANSCRIBING",
            ":now": int(time.time()),
        },
    )

    audio_key = f"pending/{video_id}.m4a"
    local_path = f"/tmp/{video_id}.m4a"

    # yt-dlp: audio-only extraction, best available quality, no video
    # stream ever touches disk (saves both time and /tmp space, which is
    # capped and shared across a Lambda execution environment's lifetime).
    subprocess.run(
        [
            "yt-dlp", "-f", "bestaudio", "-x", "--audio-format", "m4a",
            "-o", local_path, "--no-playlist",
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        check=True,
        timeout=600,
    )

    s3.upload_file(local_path, AUDIO_BUCKET, audio_key)

    job_suffix = uuid.uuid4().hex[:8]  # no hyphens -- see docstring on how transcribe_callback recovers video_id from the job name
    job_name = f"{JOB_NAME_PREFIX}-{video_id}-{job_suffix}"

    transcribe.start_transcription_job(
        TranscriptionJobName=job_name,
        IdentifyLanguage=True,  # don't assume English -- submitters may queue non-English content
        MediaFormat="m4a",
        Media={"MediaFileUri": f"s3://{AUDIO_BUCKET}/{audio_key}"},
        OutputBucketName=LANDING_BUCKET,
        OutputKey=f"transcripts_raw_asr/{video_id}/{job_name}.json",
        Settings={"ShowSpeakerLabels": False, "ChannelIdentification": False},
    )

    log.info("Started Transcribe job %s for video_id=%s", job_name, video_id)

    # No return value matters here -- the Step Functions state is paused
    # on the task token until transcribe_callback.py calls
    # send_task_success/send_task_failure.
    return {"job_name": job_name}
