############################################
# 09_lambda_transcript.tf
#
# fetch_captions needs the youtube-transcript-api PyPI package, which
# doesn't fit the "zero extra dependencies" shape of the other zip
# Lambdas here. Rather than a bespoke build step in Terraform itself
# (fragile, non-reproducible across machines), this file assumes a
# `pip install -r requirements-fetch-captions.txt -t build/fetch_captions/`
# step has already run (see README.md "Build steps") and archives
# whatever's in that populated directory. Terraform orchestrates
# infrastructure; it deliberately does NOT orchestrate Python packaging.
############################################

data "archive_file" "submit_handler" {
  type = "zip"
  source_dir  = "${path.module}/../lambda/build/submit_handler"
  output_path = "${path.module}/build/submit_handler.zip"
}

data "archive_file" "get_status" {
  type        = "zip"
  source_file = "${path.module}/../lambda/get_status.py"
  output_path = "${path.module}/build/get_status.zip"
}

data "archive_file" "start_workflow" {
  type        = "zip"
  source_file = "${path.module}/../lambda/start_workflow.py"
  output_path = "${path.module}/build/start_workflow.zip"
}

data "archive_file" "fetch_captions" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/build/fetch_captions" # pre-populated: youtube_transcript_api package + fetch_captions.py
  output_path = "${path.module}/build/fetch_captions.zip"
}

data "archive_file" "evaluate_confidence" {
  type        = "zip"
  source_file = "${path.module}/../lambda/evaluate_confidence.py"
  output_path = "${path.module}/build/evaluate_confidence.zip"
}

data "archive_file" "persist_transcript" {
  type        = "zip"
  source_file = "${path.module}/../lambda/persist_transcript.py"
  output_path = "${path.module}/build/persist_transcript.zip"
}

data "archive_file" "request_human_review" {
  type        = "zip"
  source_file = "${path.module}/../lambda/request_human_review.py"
  output_path = "${path.module}/build/request_human_review.zip"
}

data "archive_file" "review_callback" {
  type        = "zip"
  source_file = "${path.module}/../lambda/review_callback.py"
  output_path = "${path.module}/build/review_callback.zip"
}

data "archive_file" "transcribe_callback" {
  type        = "zip"
  source_file = "${path.module}/../lambda/transcribe_callback.py"
  output_path = "${path.module}/build/transcribe_callback.zip"
}

locals {
  transcript_lambda_common_env = {
    JOBS_TABLE = aws_dynamodb_table.jobs.name
  }
}

resource "aws_lambda_function" "submit_handler" {
  function_name    = "${var.project_name}-submit-handler"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "submit_handler.handler"
  runtime           = "python3.12"
  timeout           = 15
  memory_size       = 128
  filename          = data.archive_file.submit_handler.output_path
  source_code_hash  = data.archive_file.submit_handler.output_base64sha256

  environment {
    variables = merge(local.transcript_lambda_common_env, {
      QUEUE_URL = aws_sqs_queue.submissions.url
    })
  }
}

resource "aws_lambda_function" "get_status" {
  function_name    = "${var.project_name}-get-status"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "get_status.handler"
  runtime           = "python3.12"
  timeout           = 10
  memory_size       = 128
  filename          = data.archive_file.get_status.output_path
  source_code_hash  = data.archive_file.get_status.output_base64sha256

  environment {
    variables = local.transcript_lambda_common_env
  }
}

resource "aws_lambda_function" "start_workflow" {
  function_name    = "${var.project_name}-start-workflow"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "start_workflow.handler"
  runtime           = "python3.12"
  timeout           = 30
  memory_size       = 128
  filename          = data.archive_file.start_workflow.output_path
  source_code_hash  = data.archive_file.start_workflow.output_base64sha256

  environment {
    variables = merge(local.transcript_lambda_common_env, {
      STATE_MACHINE_ARN = aws_sfn_state_machine.transcript_pipeline.arn
    })
  }
}

resource "aws_lambda_event_source_mapping" "submissions_queue" {
  event_source_arn = aws_sqs_queue.submissions.arn
  function_name     = aws_lambda_function.start_workflow.arn
  batch_size        = 5
  function_response_types = ["ReportBatchItemFailures"]
}

resource "aws_lambda_function" "fetch_captions" {
  function_name    = "${var.project_name}-fetch-captions"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "fetch_captions.handler"
  runtime           = "python3.12"
  timeout           = 30
  memory_size       = 256
  filename          = data.archive_file.fetch_captions.output_path
  source_code_hash  = data.archive_file.fetch_captions.output_base64sha256

  # Reserved concurrency here is a DELIBERATE throttle, not just a cost
  # guard: it caps how many simultaneous requests hit YouTube's caption
  # endpoints through the (finite) Webshare proxy pool, regardless of
  # how many videos are queued. Uncapped concurrency here would burn
  # through the proxy pool's rate budget in a burst and cause every
  # concurrent invocation to start seeing blocks -- self-inflicted DoS
  # against your own proxy pool.
  reserved_concurrent_executions = 3

  environment {
    variables = {
      PROXY_CREDENTIALS_SECRET_ARN = aws_secretsmanager_secret.proxy_credentials.arn
    }
  }
}

resource "aws_lambda_function" "start_transcribe_job" {
  function_name = "${var.project_name}-start-transcribe-job"
  role           = aws_iam_role.lambda_transcript.arn
  timeout        = 600 # audio download + upload can genuinely take minutes for a long video
  memory_size    = 1024 # yt-dlp + ffmpeg audio extraction benefit from headroom; /tmp usage also scales with video length

  # Container image deployment -- see lambda/Dockerfile.start_transcribe_job
  # for why this one function breaks from the zip-deployment pattern.
  package_type = "Image"
  image_uri    = var.start_transcribe_job_image_uri

  environment {
    variables = merge(local.transcript_lambda_common_env, {
      AUDIO_STAGING_BUCKET = aws_s3_bucket.audio_staging.bucket
      LANDING_BUCKET        = aws_s3_bucket.landing.bucket
    })
  }
}

variable "start_transcribe_job_image_uri" {
  type        = string
  description = "ECR image URI for the start_transcribe_job container image. Build & push per lambda/Dockerfile.start_transcribe_job before first apply."
}

resource "aws_lambda_function" "evaluate_confidence" {
  function_name    = "${var.project_name}-evaluate-confidence"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "evaluate_confidence.handler"
  runtime           = "python3.12"
  timeout           = 10
  memory_size       = 128
  filename          = data.archive_file.evaluate_confidence.output_path
  source_code_hash  = data.archive_file.evaluate_confidence.output_base64sha256

  environment {
    variables = {
      ASR_CONFIDENCE_THRESHOLD = tostring(var.asr_confidence_review_threshold)
    }
  }
}

resource "aws_lambda_function" "persist_transcript" {
  function_name    = "${var.project_name}-persist-transcript"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "persist_transcript.handler"
  runtime           = "python3.12"
  timeout           = 30
  memory_size       = 256
  filename          = data.archive_file.persist_transcript.output_path
  source_code_hash  = data.archive_file.persist_transcript.output_base64sha256

  environment {
    variables = merge(local.transcript_lambda_common_env, {
      LANDING_BUCKET = aws_s3_bucket.landing.bucket
    })
  }
}

resource "aws_lambda_function" "request_human_review" {
  function_name    = "${var.project_name}-request-human-review"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "request_human_review.handler"
  runtime           = "python3.12"
  timeout           = 30
  memory_size       = 256
  filename          = data.archive_file.request_human_review.output_path
  source_code_hash  = data.archive_file.request_human_review.output_base64sha256

  environment {
    variables = {
      REVIEWS_TABLE           = aws_dynamodb_table.reviews.name
      LANDING_BUCKET           = aws_s3_bucket.landing.bucket
      REVIEW_TOPIC_ARN         = aws_sns_topic.review_requests.arn
      REVIEW_HMAC_SECRET_ARN   = aws_secretsmanager_secret.review_hmac_key.arn
      API_BASE_URL             = aws_apigatewayv2_stage.default.invoke_url
      REVIEW_TOKEN_TTL_HOURS   = tostring(var.review_token_ttl_hours)
    }
  }
}

resource "aws_lambda_function" "review_callback" {
  function_name    = "${var.project_name}-review-callback"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "review_callback.handler"
  runtime           = "python3.12"
  timeout           = 15
  memory_size       = 128
  filename          = data.archive_file.review_callback.output_path
  source_code_hash  = data.archive_file.review_callback.output_base64sha256

  environment {
    variables = {
      REVIEWS_TABLE          = aws_dynamodb_table.reviews.name
      LANDING_BUCKET          = aws_s3_bucket.landing.bucket
      REVIEW_HMAC_SECRET_ARN  = aws_secretsmanager_secret.review_hmac_key.arn
    }
  }
}

resource "aws_lambda_function" "transcribe_callback" {
  function_name    = "${var.project_name}-transcribe-callback"
  role              = aws_iam_role.lambda_transcript.arn
  handler           = "transcribe_callback.handler"
  runtime           = "python3.12"
  timeout           = 30
  memory_size       = 256
  filename          = data.archive_file.transcribe_callback.output_path
  source_code_hash  = data.archive_file.transcribe_callback.output_base64sha256

  environment {
    variables = merge(local.transcript_lambda_common_env, {
      LANDING_BUCKET = aws_s3_bucket.landing.bucket
    })
  }
}

resource "aws_cloudwatch_log_group" "transcript_lambdas" {
  for_each = toset([
    "submit-handler", "get-status", "start-workflow", "fetch-captions",
    "start-transcribe-job", "evaluate-confidence", "persist-transcript",
    "request-human-review", "review-callback", "transcribe-callback",
  ])
  name              = "/aws/lambda/${var.project_name}-${each.value}"
  retention_in_days = 14
}
