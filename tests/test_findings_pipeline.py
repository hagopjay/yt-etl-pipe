"""Executable proof for the pipeline/IaC findings in the assessment.
`test_bug_*` passes while the bug exists; invert it when fixing."""

import json
import re
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SFN_PAYLOAD_LIMIT = 256 * 1024  # Step Functions max state input/output, bytes


# ---------------------------------------------------------------- payload size
def _caption_segments(minutes: float, secs_per_segment=2.5, chars_per_segment=42):
    n = int(minutes * 60 / secs_per_segment)
    return [{"start": round(i * secs_per_segment, 3), "duration": secs_per_segment,
             "text": "x" * chars_per_segment} for i in range(n)]


def _state_after_fetch(minutes):
    """State document entering MergeFetchResult: original input + flags +
    the whole fetch payload under $.fetch_result."""
    payload = {"status": "SUCCESS", "video_id": "dQw4w9WgXcQ", "language": "en",
               "is_generated": True, "transcript_source": "caption_auto",
               "segments": _caption_segments(minutes)}
    return {"video_id": "dQw4w9WgXcQ", "source_url": "https://youtu.be/dQw4w9WgXcQ",
            "respect_disabled": True, "asr_enabled": True,
            "fetch_result": {"payload": payload}}


def test_bug_long_videos_exceed_step_functions_payload_limit():
    """Segments ride through the state machine inline. Typical auto-caption
    density (~2.5 s / ~42 chars per snippet) crosses 256 KB a little past
    the 2-hour mark -- podcasts, lectures and streams in a watch history
    hit this and die with States.DataLimitExceeded."""
    sizes = {m: len(json.dumps(_state_after_fetch(m)).encode()) for m in (30, 60, 120, 180)}
    assert sizes[60] < SFN_PAYLOAD_LIMIT
    assert sizes[180] > SFN_PAYLOAD_LIMIT
    # Find the break-even length for the report.
    lo, hi = 60, 400
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if len(json.dumps(_state_after_fetch(mid)).encode()) > SFN_PAYLOAD_LIMIT:
            hi = mid
        else:
            lo = mid
    print(f"\n[payload] state exceeds 256 KB at ~{hi} minutes of captions")
    assert 90 <= hi <= 240


# ------------------------------------------------------ fetch_captions routing
@pytest.fixture
def fetch_captions():
    import fetch_captions as fc
    return fc


@pytest.mark.parametrize("exc_name", ["AgeRestricted", "InvalidVideoId", "VideoUnplayable"])
def test_bug_permanent_errors_are_retried_as_blocked(fetch_captions, exc_name):
    """These are permanent, but they fall into the CouldNotRetrieveTranscript
    catch-all and become BlockedRetryable -> 4 backoff retries
    (30+60+120+240 s, ~7.5 min of proxy budget) -> BLOCKED_PERMANENTLY +
    an ops page. Watch histories contain lots of age-restricted and
    since-deleted/members-only videos."""
    import youtube_transcript_api._errors as errors
    exc = getattr(errors, exc_name)("dQw4w9WgXcQ") if exc_name != "VideoUnplayable" else \
        errors.VideoUnplayable("dQw4w9WgXcQ", "reason", [])
    client = mock.Mock()
    client.fetch.side_effect = exc
    with mock.patch.object(fetch_captions, "_get_client", return_value=client):
        with pytest.raises(fetch_captions.BlockedRetryable):
            fetch_captions.handler({"video_id": "dQw4w9WgXcQ"}, None)


def test_status_contract_is_complete_for_disabled(fetch_captions):
    import youtube_transcript_api._errors as errors
    client = mock.Mock()
    client.fetch.side_effect = errors.TranscriptsDisabled("dQw4w9WgXcQ")
    with mock.patch.object(fetch_captions, "_get_client", return_value=client):
        out = fetch_captions.handler({"video_id": "dQw4w9WgXcQ"}, None)
    assert out["status"] == "DISABLED"
    assert {"language", "segments", "is_generated", "transcript_source"} <= out.keys()


# -------------------------------------------------------- transcribe parser
def _item(word, start, conf=0.9):
    return {"type": "pronunciation", "start_time": str(start), "end_time": str(start + 0.4),
            "alternatives": [{"content": word, "confidence": str(conf)}]}


def test_transcribe_parser_buckets_and_confidence():
    import transcribe_callback as tc
    items = [_item(f"w{i}", i * 1.0, conf=0.5 if i % 2 else 1.0) for i in range(25)]
    items.insert(3, {"type": "punctuation", "alternatives": [{"content": ","}]})
    segments, confs = tc._parse_transcribe_output({"results": {"items": items}})
    assert [s["start"] for s in segments] == [0.0, 10.0, 20.0]
    assert segments[0]["text"].startswith("w0 w1 w2, w3")
    assert len(confs) == 25
    # Minor: the final segment's duration is hard-coded to 0.0.
    assert segments[-1]["duration"] == 0.0


def test_video_id_round_trips_through_job_name_even_with_hyphens():
    import transcribe_callback as tc
    assert tc._video_id_from_job_name("transcript-a-b-c_d-EFGH-1a2b3c4d") == "a-b-c_d-EFGH"


# ------------------------------------------------------------- URL parsing
@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
])
def test_url_shapes_supported(url, expected):
    from youtube_url import extract_video_id
    assert extract_video_id(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/live/dQw4w9WgXcQ?si=x",          # live / premiere links
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",     # privacy-enhanced embeds
])
def test_bug_url_shapes_not_supported(url):
    from youtube_url import extract_video_id
    assert extract_video_id(url) is None


# ----------------------------------------------------- IaC / ASL static checks
def _tf(name):
    return (ROOT / "terraform" / name).read_text()


def _sfn_policy_block():
    iam = _tf("03_iam.tf")
    start = iam.index('data "aws_iam_policy_document" "sfn_perms"')
    end = iam.index('resource "aws_iam_role_policy" "sfn"')
    return iam[start:end]


def test_bug_state_machine_role_cannot_run_its_own_terminal_states():
    """Every Mark* state uses arn:aws:states:::aws-sdk:dynamodb:updateItem
    and NotifyOps uses sns:publish, but the SFN role only grants
    lambda:InvokeFunction + log delivery. Every non-happy path fails with
    AccessDenied, leaving the job row in PROCESSING/TRANSCRIBING forever."""
    asl = (ROOT / "stepfunctions" / "transcript_pipeline.asl.json.tftpl").read_text()
    assert "aws-sdk:dynamodb:updateItem" in asl and "sns:publish" in asl
    block = _sfn_policy_block()
    assert "dynamodb:UpdateItem" not in block
    assert "sns:Publish" not in block
    # X-Ray tracing is enabled on the state machine, but no xray:Put* grant either.
    assert "tracing_configuration" in _tf("10_stepfunctions.tf") and "xray:" not in block


def test_bug_all_transcript_lambdas_share_one_role():
    """get_status.py's docstring claims GetItem-only least privilege; in
    Terraform every transcript Lambda (including the two public API
    handlers) shares a role with SendTaskSuccess on *, Transcribe, both
    secrets and S3 write."""
    tf = _tf("09_lambda_transcript.tf")
    roles = re.findall(r"role\s*=\s*aws_iam_role\.(\w+)\.arn", tf)
    assert len(roles) == 10 and set(roles) == {"lambda_transcript"}


def test_bug_awaiting_review_status_is_never_written():
    """The frontend and submit_handler know about AWAITING_REVIEW and
    BLOCKED_RETRYING, but no code path ever sets them, so a video parked
    on a 72-hour human review shows TRANSCRIBING the whole time."""
    writers = [p for p in (ROOT / "lambda").glob("*.py") if p.name != "submit_handler.py"]
    writers.append(ROOT / "stepfunctions" / "transcript_pipeline.asl.json.tftpl")
    for status in ("AWAITING_REVIEW", "BLOCKED_RETRYING"):
        assert f'"{status}"' in (ROOT / "lambda" / "submit_handler.py").read_text()  # read...
        assert not any(status in p.read_text() for p in writers)                   # ...never written


def test_bug_disabled_by_creator_is_recorded_as_no_captions():
    """MarkDisabledByCreator writes NO_CAPTIONS, which submit_handler treats
    as re-submittable -- the 'respect the creator' signal is lost and the
    video will be retried (or would be, if resubmission worked)."""
    asl = json.loads(re.sub(r"\$\{[a-z_]+\}", "1",
                            (ROOT / "stepfunctions" / "transcript_pipeline.asl.json.tftpl").read_text()))
    st = asl["States"]["MarkDisabledByCreator"]
    assert st["Parameters"]["ExpressionAttributeValues"][":status"]["S"] == "NO_CAPTIONS"


def test_bug_fetch_captions_throttles_are_not_retried():
    """reserved_concurrent_executions = 3, but the FetchCaptions Retry list
    omits Lambda.TooManyRequestsException. Bulk-seeding a watch history
    starts dozens of executions at once; every throttled invoke falls to
    the States.ALL catch -> BLOCKED_PERMANENTLY."""
    assert "reserved_concurrent_executions = 3" in _tf("09_lambda_transcript.tf")
    asl = (ROOT / "stepfunctions" / "transcript_pipeline.asl.json.tftpl").read_text()
    fetch = asl[asl.index('"FetchCaptions"'):asl.index('"MergeFetchResult"')]
    assert "TooManyRequestsException" not in fetch


def test_bug_materialize_job_bookmarks_plus_overwrite():
    """Job bookmarks give the job only NEW rows since the last run; the job
    then writes mode('overwrite'). Second run replaces the analytics table
    with just the delta (and joins it to an empty category delta)."""
    glue_tf = _tf("04_glue.tf")
    mat = glue_tf[glue_tf.index('resource "aws_glue_job" "materialize_categories"'):]
    assert '"job-bookmark-enable"' in mat.split("worker_type")[0]
    job = (ROOT / "glue" / "materialize_categories_job.py").read_text()
    assert 'transformation_ctx="stats_source"' in job and 'mode("overwrite")' in job


def test_bug_cleansed_video_statistics_is_never_cataloged():
    """materialize_categories reads table video_statistics from the catalog,
    but raw_to_parquet writes plain Parquet to S3 and nothing (no crawler,
    no enableUpdateCatalog sink) registers it."""
    raw_job = (ROOT / "glue" / "raw_to_parquet_job.py").read_text()
    assert ".parquet(args[\"target_s3_path\"])" in raw_job
    assert "enableUpdateCatalog" not in raw_job and "getSink" not in raw_job
    crawler_targets = re.findall(r'path\s*=\s*"([^"]+)"', _tf("04_glue.tf"))
    assert not any("cleansed" in t for t in crawler_targets)
