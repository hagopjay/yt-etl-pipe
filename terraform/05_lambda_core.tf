############################################
# 05_lambda_core.tf — the base project's Lambda: raw category JSON -> Parquet
############################################

data "archive_file" "json_to_parquet" {
  type        = "zip"
  source_file = "${path.module}/../lambda/json_to_parquet.py"
  output_path = "${path.module}/build/json_to_parquet.zip"
}

# AWS Data Wrangler (awswrangler) ships as a published Lambda layer per
# region/runtime -- using the published ARN instead of building a custom
# layer avoids maintaining a ~50MB dependency bundle ourselves. Verify the
# current ARN for your region/runtime at the aws-sdk-pandas GitHub releases
# page before applying; these ARNs are versioned and region-specific.
variable "awswrangler_layer_arn" {
  type        = string
  default     = "arn:aws:lambda:us-east-1:336392948345:layer:AWSSDKPandas-Python312:15"
  description = "Published AWS SDK for pandas (fka Data Wrangler) layer ARN. Verify current version for your region."
}

resource "aws_lambda_function" "json_to_parquet" {
  function_name = "${var.project_name}-json-to-parquet"
  role          = aws_iam_role.lambda_json_to_parquet.arn
  handler       = "json_to_parquet.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512 # Pandas + a full-file dataframe load needs headroom beyond the transcript lambdas' 256MB

  filename         = data.archive_file.json_to_parquet.output_path
  source_code_hash = data.archive_file.json_to_parquet.output_base64sha256

  layers = [var.awswrangler_layer_arn]

  environment {
    variables = {
      CLEANSED_BUCKET = aws_s3_bucket.cleansed.bucket
      GLUE_DATABASE   = aws_glue_catalog_database.raw.name
      GLUE_TABLE      = "video_category_reference"
    }
  }
}

resource "aws_lambda_permission" "allow_s3_invoke" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.json_to_parquet.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.landing.arn
}

resource "aws_s3_bucket_notification" "landing_trigger" {
  bucket = aws_s3_bucket.landing.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.json_to_parquet.arn
    events               = ["s3:ObjectCreated:*"]
    filter_prefix        = "category_reference/"
    filter_suffix        = ".json"
  }

  depends_on = [aws_lambda_permission.allow_s3_invoke]
}

resource "aws_cloudwatch_log_group" "json_to_parquet" {
  name              = "/aws/lambda/${aws_lambda_function.json_to_parquet.function_name}"
  retention_in_days = 14
}
