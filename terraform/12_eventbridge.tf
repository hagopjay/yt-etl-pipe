############################################
# 12_eventbridge.tf
#
# AWS Transcribe emits a "Transcribe Job State Change" event on its own
# default EventBridge bus whenever a batch job finishes (no explicit
# subscription/opt-in needed on Transcribe's side -- it's always emitted;
# this rule just chooses to listen). This is what lets
# start_transcribe_job.py return immediately after STARTING a job instead
# of blocking a Lambda invocation on a poll loop -- see that file's
# docstring.
############################################

resource "aws_cloudwatch_event_rule" "transcribe_job_state_change" {
  name = "${var.project_name}-transcribe-job-state-change"

  event_pattern = jsonencode({
    source      = ["aws.transcribe"]
    detail-type = ["Transcribe Job State Change"]
    detail = {
      TranscriptionJobStatus = ["COMPLETED", "FAILED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "transcribe_callback" {
  rule = aws_cloudwatch_event_rule.transcribe_job_state_change.name
  arn  = aws_lambda_function.transcribe_callback.arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 3
  }

  dead_letter_config {
    arn = aws_sqs_queue.eventbridge_dlq.arn
  }
}

# If transcribe_callback itself fails repeatedly (e.g. a bug, or the
# jobs-table lookup misses), the underlying Step Functions execution
# would otherwise hang on its task token FOREVER with no visible error --
# this DLQ is what surfaces that failure instead of a silent stuck
# execution nobody notices until a human asks "where's my transcript."
resource "aws_sqs_queue" "eventbridge_dlq" {
  name                      = "${var.project_name}-eventbridge-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue_policy" "eventbridge_dlq" {
  queue_url = aws_sqs_queue.eventbridge_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.eventbridge_dlq.arn
      Condition = {
        ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.transcribe_job_state_change.arn }
      }
    }]
  })
}

resource "aws_lambda_permission" "eventbridge_invoke_transcribe_callback" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.transcribe_callback.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.transcribe_job_state_change.arn
}
