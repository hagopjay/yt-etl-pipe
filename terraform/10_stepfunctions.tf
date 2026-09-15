############################################
# 10_stepfunctions.tf
############################################

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${var.project_name}-transcript-pipeline"
  retention_in_days = 30
}

resource "aws_sfn_state_machine" "transcript_pipeline" {
  name     = "${var.project_name}-transcript-pipeline"
  role_arn = aws_iam_role.sfn.arn
  type     = "STANDARD" # NOT Express: this workflow can legitimately pause for hours (human review wait), which Express's 5-minute max duration cannot accommodate

  definition = templatefile("${path.module}/../stepfunctions/transcript_pipeline.asl.json.tftpl", {
    fetch_captions_arn        = aws_lambda_function.fetch_captions.arn
    start_transcribe_job_arn  = aws_lambda_function.start_transcribe_job.arn
    evaluate_confidence_arn   = aws_lambda_function.evaluate_confidence.arn
    request_human_review_arn  = aws_lambda_function.request_human_review.arn
    persist_transcript_arn    = aws_lambda_function.persist_transcript.arn
    jobs_table_name             = aws_dynamodb_table.jobs.name
    ops_alarms_topic_arn        = aws_sns_topic.ops_alarms.arn
    max_fetch_retry_attempts               = var.max_fetch_retry_attempts
    review_timeout_seconds                 = var.review_token_ttl_hours * 3600
    respect_creator_disabled_captions      = var.respect_creator_disabled_captions
    enable_asr_fallback                    = var.enable_asr_fallback
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                    = "ALL"
  }

  tracing_configuration {
    enabled = true # X-Ray -- with a workflow this branchy (20 states, 3 external async waits), tracing pays for itself the first time you have to debug a stuck execution
  }
}
