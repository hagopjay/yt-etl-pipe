############################################
# 11_apigateway.tf
#
# HTTP API (not REST API) throughout -- this project has no need for
# REST API's extra features (request validators, usage plans, API keys)
# and HTTP API is materially cheaper per-request and lower-latency,
# which matters specifically for review_callback: that's the endpoint a
# human's mail client hits when they click an emailed link, and every
# extra 100ms there is UX, not just cost.
############################################

resource "aws_apigatewayv2_api" "this" {
  name          = "${var.project_name}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"] # tighten to the actual frontend origin in a real deployment; "*" kept here so frontend/submit.html works from a local file:// or any static host without extra config
    allow_methods = ["GET", "POST"]
    allow_headers = ["content-type"]
  }
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      integrationErr = "$context.integrationErrorMessage"
      responseLatency = "$context.responseLatency"
    })
  }
}

resource "aws_cloudwatch_log_group" "api_gw" {
  name              = "/aws/apigateway/${var.project_name}"
  retention_in_days = 30
}

# ---------- POST /submissions ----------
resource "aws_apigatewayv2_integration" "submit_handler" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.submit_handler.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "submit" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /submissions"
  target    = "integrations/${aws_apigatewayv2_integration.submit_handler.id}"
}

resource "aws_lambda_permission" "apigw_submit" {
  statement_id  = "AllowAPIGatewaySubmit"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.submit_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# ---------- GET /submissions/{video_id} ----------
resource "aws_apigatewayv2_integration" "get_status" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.get_status.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_status" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /submissions/{video_id}"
  target    = "integrations/${aws_apigatewayv2_integration.get_status.id}"
}

resource "aws_lambda_permission" "apigw_get_status" {
  statement_id  = "AllowAPIGatewayGetStatus"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.get_status.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# ---------- GET /review (human approve/reject links) ----------
resource "aws_apigatewayv2_integration" "review_callback" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.review_callback.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "review" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /review"
  target    = "integrations/${aws_apigatewayv2_integration.review_callback.id}"
}

resource "aws_lambda_permission" "apigw_review" {
  statement_id  = "AllowAPIGatewayReview"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.review_callback.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}
