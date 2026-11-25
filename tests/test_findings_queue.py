"""Executable proof for the queue-layer findings in the assessment.

These tests describe the code AS IT IS TODAY. A test named
`test_bug_*` passes when the bug is present; flip its assertion once the
fix lands so it becomes a regression guard.
"""

import importlib
import json
import os

import boto3
import pytest
from moto import mock_aws

ASL_STUB = json.dumps({"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}})


@pytest.fixture
def aws():
    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="jobs",
            KeySchema=[{"AttributeName": "video_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "video_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs")
        q = sqs.create_queue(
            QueueName="submissions.fifo",
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true"},
        )["QueueUrl"]
        iam = boto3.client("iam")
        role = iam.create_role(RoleName="sfn", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        sm = boto3.client("stepfunctions").create_state_machine(
            name="pipeline", definition=ASL_STUB, roleArn=role
        )["stateMachineArn"]
        os.environ["QUEUE_URL"] = q
        os.environ["STATE_MACHINE_ARN"] = sm
        import submit_handler
        import start_workflow
        importlib.reload(submit_handler)
        importlib.reload(start_workflow)
        yield {"sqs": sqs, "queue": q, "submit": submit_handler, "start": start_workflow,
               "table": boto3.resource("dynamodb").Table("jobs")}


def _submit(aws, url):
    resp = aws["submit"].handler({"body": json.dumps({"url": url})}, None)
    return resp["statusCode"], json.loads(resp["body"])


def _drain(aws):
    """Pull everything off the FIFO queue and feed it to start_workflow,
    the way the Lambda event source mapping would."""
    msgs = aws["sqs"].receive_message(QueueUrl=aws["queue"], MaxNumberOfMessages=10).get("Messages", [])
    records = [{"messageId": m["MessageId"], "body": m["Body"]} for m in msgs]
    out = aws["start"].handler({"Records": records}, None)
    for m in msgs:
        aws["sqs"].delete_message(QueueUrl=aws["queue"], ReceiptHandle=m["ReceiptHandle"])
    return len(records), out


def test_happy_path_first_submission_starts_execution(aws):
    code, body = _submit(aws, "https://youtu.be/dQw4w9WgXcQ")
    assert code == 202 and body["status"] == "QUEUED"
    n, out = _drain(aws)
    assert n == 1 and out["batchItemFailures"] == []
    assert aws["table"].get_item(Key={"video_id": "dQw4w9WgXcQ"})["Item"]["status"] == "PROCESSING"


def test_bug_resubmitting_a_no_captions_video_strands_it_in_queued(aws):
    """submit_handler deliberately allows NO_CAPTIONS / REJECTED videos to
    be resubmitted, but start_workflow reuses the deterministic execution
    name vid-{video_id}. Standard-workflow names can't be reused for 90
    days, so StartExecution raises ExecutionAlreadyExists, start_workflow
    swallows it as 'idempotent success', and the job row sits in QUEUED
    with no execution behind it."""
    vid = "dQw4w9WgXcQ"
    _submit(aws, f"https://www.youtube.com/watch?v={vid}")
    _drain(aws)
    # Pipeline finishes with no captions (what MarkNoCaptionsAvailable writes).
    aws["table"].update_item(Key={"video_id": vid}, UpdateExpression="SET #s = :s",
                             ExpressionAttributeNames={"#s": "status"},
                             ExpressionAttributeValues={":s": "NO_CAPTIONS"})
    # Use a different URL shape so FIFO content-based dedup can't hide the bug.
    code, body = _submit(aws, f"https://youtu.be/{vid}")
    assert code == 202 and body["status"] == "QUEUED"
    n, out = _drain(aws)
    assert n == 1 and out["batchItemFailures"] == []          # reported as success...
    status = aws["table"].get_item(Key={"video_id": vid})["Item"]["status"]
    assert status == "QUEUED"                                   # ...but nothing is running


@pytest.mark.parametrize("terminal", ["UNAVAILABLE", "BLOCKED_PERMANENTLY", "TRANSCRIBE_FAILED", "REVIEW_EXPIRED"])
def test_bug_failure_states_can_never_be_retried(aws, terminal):
    """Only NO_CAPTIONS/REJECTED get the re-queue branch. A video that hit
    BLOCKED_PERMANENTLY because the proxy pool was down for an hour is
    stuck forever -- the API just says 'Already known to the system.'"""
    vid = "dQw4w9WgXcQ"
    aws["table"].put_item(Item={"video_id": vid, "status": terminal})
    code, body = _submit(aws, f"https://youtu.be/{vid}")
    assert code == 202 and body["message"] == "Already known to the system."
    n, _ = _drain(aws)
    assert n == 0
