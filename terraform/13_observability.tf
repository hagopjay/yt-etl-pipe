############################################
# 13_observability.tf
############################################

resource "aws_athena_workgroup" "this" {
  name = "${var.project_name}-workgroup"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.bucket}/results/"
    }

    bytes_scanned_cutoff_per_query = 5368709120 # 5 GB -- same FinOps guardrail pattern as the accidents-platform project
  }
}

resource "aws_cloudwatch_metric_alarm" "sfn_execution_failures" {
  alarm_name          = "${var.project_name}-sfn-execution-failures"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.transcript_pipeline.arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "sfn_executions_timed_out" {
  alarm_name          = "${var.project_name}-sfn-executions-timed-out"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsTimedOut"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.transcript_pipeline.arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "fetch_captions_errors" {
  alarm_name          = "${var.project_name}-fetch-captions-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.fetch_captions.function_name }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 5 # a handful of individual failures is expected (blocked/no-captions videos); a burst signals the proxy pool itself may be exhausted
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "submissions_dlq_depth" {
  alarm_name          = "${var.project_name}-submissions-dlq-depth"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.submissions_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold" # any message at all in a DLQ warrants a look
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
}

# A small CloudWatch dashboard, per the original architecture diagram's
# explicit "Monitoring / Alert: AWS CloudWatch" box -- gives a single
# glanceable view without needing Grafana/QuickSight open.
resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = "${var.project_name}-overview"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6,
        properties = {
          title   = "Step Functions: started vs. succeeded vs. failed"
          region  = var.aws_region
          metrics = [
            ["AWS/States", "ExecutionsStarted", "StateMachineArn", aws_sfn_state_machine.transcript_pipeline.arn],
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", aws_sfn_state_machine.transcript_pipeline.arn],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", aws_sfn_state_machine.transcript_pipeline.arn],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6,
        properties = {
          title   = "Caption fetch Lambda: invocations vs. errors"
          region  = var.aws_region
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.fetch_captions.function_name],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.fetch_captions.function_name],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6,
        properties = {
          title   = "Submissions queue depth"
          region  = var.aws_region
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.submissions.name],
          ]
        }
      },
    ]
  })
}
