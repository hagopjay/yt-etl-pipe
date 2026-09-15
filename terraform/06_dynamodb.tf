############################################
# 06_dynamodb.tf
#
# Two tables, deliberately not one:
#   jobs    -- long-lived record of every submitted video's processing
#              state. PK = video_id. This is what makes resubmission
#              idempotent (see submit_handler.py) and what the frontend
#              polls for status.
#   reviews -- short-lived, TTL-bound mapping from an opaque review_id to
#              a Step Functions task token, used ONLY by the human-review
#              approve/reject flow. Kept separate from jobs on purpose:
#              a review record contains a live task token (a bearer
#              credential for resuming a specific execution) and needs a
#              tighter blast radius, single-use consumption semantics,
#              and automatic expiry that the jobs table has no business
#              inheriting.
############################################

resource "aws_dynamodb_table" "jobs" {
  name         = "${var.project_name}-jobs"
  billing_mode = "PAY_PER_REQUEST" # submission volume is human-paced and bursty, not steady -- provisioned capacity would mean paying for idle most of the time
  hash_key     = "video_id"

  attribute {
    name = "video_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true # this table is the only source of truth for "did we already process this video" -- worth protecting
  }
}

resource "aws_dynamodb_table" "reviews" {
  name         = "${var.project_name}-reviews"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "review_id"

  attribute {
    name = "review_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
