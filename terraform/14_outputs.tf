############################################
# 14_outputs.tf
############################################

output "api_base_url" {
  value       = aws_apigatewayv2_stage.default.invoke_url
  description = "Base URL for POST /submissions, GET /submissions/{video_id}, GET /review. Point frontend/submit.html's API_BASE at this."
}

output "landing_bucket" {
  value = aws_s3_bucket.landing.bucket
}

output "cleansed_bucket" {
  value = aws_s3_bucket.cleansed.bucket
}

output "analytics_bucket" {
  value = aws_s3_bucket.analytics.bucket
}

output "jobs_table_name" {
  value = aws_dynamodb_table.jobs.name
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.transcript_pipeline.arn
}

output "proxy_credentials_secret_arn" {
  value       = aws_secretsmanager_secret.proxy_credentials.arn
  description = "Set the real Webshare username/password here via `aws secretsmanager put-secret-value` before submitting any videos -- see docs/ARCHITECTURE_INSIGHTS.md."
}

output "athena_workgroup" {
  value = aws_athena_workgroup.this.name
}

output "raw_glue_database" {
  value = aws_glue_catalog_database.raw.name
}

output "analytics_glue_database" {
  value = aws_glue_catalog_database.analytics.name
}
