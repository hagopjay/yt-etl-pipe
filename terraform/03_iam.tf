############################################
# 03_iam.tf
############################################

# ---------- Glue ----------
data "aws_iam_policy_document" "glue_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "glue" {
  name               = "${var.project_name}-glue-role"
  assume_role_policy = data.aws_iam_policy_document.glue_assume.json
}

resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

data "aws_iam_policy_document" "glue_s3" {
  statement {
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.landing.arn, "${aws_s3_bucket.landing.arn}/*"]
  }
  statement {
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.cleansed.arn, "${aws_s3_bucket.cleansed.arn}/*",
                 aws_s3_bucket.analytics.arn, "${aws_s3_bucket.analytics.arn}/*"]
  }
}

resource "aws_iam_role_policy" "glue_s3" {
  name   = "${var.project_name}-glue-s3-policy"
  role   = aws_iam_role.glue.id
  policy = data.aws_iam_policy_document.glue_s3.json
}

# ---------- Core pipeline Lambda (JSON -> Parquet via Data Wrangler) -----
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_json_to_parquet" {
  name               = "${var.project_name}-json-to-parquet-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_json_to_parquet_perms" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.landing.arn}/*"]
  }
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cleansed.arn}/*"]
  }
  statement {
    actions = [
      "glue:GetTable", "glue:GetDatabase", "glue:CreateTable", "glue:UpdateTable",
      "glue:GetPartitions", "glue:BatchCreatePartition",
    ]
    resources = ["*"] # Glue Catalog resources don't support fine ARN scoping for these calls cleanly across table lifecycle; scoped instead by which DB this role is ever wired to at the application layer
  }
  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "lambda_json_to_parquet" {
  name   = "${var.project_name}-json-to-parquet-policy"
  role   = aws_iam_role.lambda_json_to_parquet.id
  policy = data.aws_iam_policy_document.lambda_json_to_parquet_perms.json
}

# ---------- Transcript-queue Lambdas (shared role, narrowly scoped per
# resource -- one role because these functions form one cohesive
# subsystem and share the same trust boundary; a real hardening pass
# would split submit/review from fetch/persist since submit_handler and
# review_callback are the only two with public (API Gateway) ingress) --
resource "aws_iam_role" "lambda_transcript" {
  name               = "${var.project_name}-transcript-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_transcript_perms" {
  statement {
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.jobs.arn, aws_dynamodb_table.reviews.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.submissions.arn]
  }
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.submissions.arn]
  }
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.transcript_pipeline.arn]
  }
  statement {
    actions   = ["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"]
    resources = ["*"] # task tokens are opaque and not resource-scopable; access is instead gated at the application layer (HMAC review tokens, DynamoDB single-use consumption)
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.proxy_credentials.arn, aws_secretsmanager_secret.review_hmac_key.arn]
  }
  statement {
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.landing.arn}/*", "${aws_s3_bucket.audio_staging.arn}/*"]
  }
  statement {
    actions   = ["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"]
    resources = ["*"] # Transcribe job ARNs aren't known until creation; AWS's own managed policy for this service uses "*" as well
  }
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.ops_alarms.arn, aws_sns_topic.review_requests.arn]
  }
  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "lambda_transcript" {
  name   = "${var.project_name}-transcript-lambda-policy"
  role   = aws_iam_role.lambda_transcript.id
  policy = data.aws_iam_policy_document.lambda_transcript_perms.json
}

# ---------- Step Functions execution role --------------------------------
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${var.project_name}-sfn-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn_perms" {
  statement {
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.fetch_captions.arn,
      aws_lambda_function.start_transcribe_job.arn,
      aws_lambda_function.evaluate_confidence.arn,
      aws_lambda_function.persist_transcript.arn,
      aws_lambda_function.request_human_review.arn,
      "${aws_lambda_function.fetch_captions.arn}:*",
      "${aws_lambda_function.start_transcribe_job.arn}:*",
      "${aws_lambda_function.evaluate_confidence.arn}:*",
      "${aws_lambda_function.persist_transcript.arn}:*",
      "${aws_lambda_function.request_human_review.arn}:*",
    ]
  }
  statement {
    actions   = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
                 "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
                 "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"]
    resources = ["*"] # required verbatim set for Step Functions CloudWatch Logs integration per AWS docs
  }
}

resource "aws_iam_role_policy" "sfn" {
  name   = "${var.project_name}-sfn-policy"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn_perms.json
}

# ---------- EventBridge -> Lambda (Transcribe job state change) ----------
data "aws_iam_policy_document" "eventbridge_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}
