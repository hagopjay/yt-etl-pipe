############################################
# 07_sqs_sns.tf
#
# FIFO queue, MessageGroupId = video_id:
#   Standard SQS gives no ordering/dedup guarantee, which is fine for the
#   accidents-platform ingest stream (order-independent telemetry) but
#   wrong here: if the same video_id is submitted twice within seconds
#   (a user double-clicking "submit", or the frontend retrying on a
#   flaky connection), a Standard queue would happily hand both messages
#   to two concurrent consumers, and you'd run two Step Functions
#   executions -- two proxy-fetch attempts, possibly two Transcribe jobs,
#   racing to write the same DynamoDB item. FIFO with per-video-id
#   MessageGroupId guarantees SQS itself serializes same-video messages,
#   and ContentBasedDeduplication collapses exact-duplicate submissions
#   within the 5-minute dedup window without any application code.
############################################

resource "aws_sqs_queue" "submissions_dlq" {
  name                      = "${var.project_name}-submissions-dlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600 # 14 days -- long enough to investigate and manually replay a batch of failures
}

resource "aws_sqs_queue" "submissions" {
  name                        = "${var.project_name}-submissions.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = 90 # comfortably longer than start_workflow's expected execution time

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.submissions_dlq.arn
    maxReceiveCount      = 3
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "submissions_dlq" {
  queue_url = aws_sqs_queue.submissions_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns    = [aws_sqs_queue.submissions.arn]
  })
}

resource "aws_sns_topic" "ops_alarms" {
  name              = "${var.project_name}-ops-alarms"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic" "review_requests" {
  name              = "${var.project_name}-review-requests"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "ops_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.ops_alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_subscription" "review_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.review_requests.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
