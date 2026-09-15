############################################
# 02_s3.tf — Landing / Cleansed-Enriched / Analytics-Reporting
#
# Same medallion shape as the project diagram. One addition versus the
# brief: transcripts land in the SAME landing bucket under their own
# prefix (landing/transcripts/) rather than a separate bucket. This
# means the existing Glue crawler + Lambda JSON->Parquet pattern the
# base project already builds is reused for transcripts for free --
# a new data source, zero new storage-layer infrastructure. This is the
# single biggest "amplify, don't bolt on" decision in this build.
############################################

resource "aws_s3_bucket" "landing" {
  bucket = "${var.project_name}-landing-${random_id.suffix.hex}"
}

resource "aws_s3_bucket" "cleansed" {
  bucket = "${var.project_name}-cleansed-${random_id.suffix.hex}"
}

resource "aws_s3_bucket" "analytics" {
  bucket = "${var.project_name}-analytics-${random_id.suffix.hex}"
}

resource "aws_s3_bucket" "athena_results" {
  bucket = "${var.project_name}-athena-results-${random_id.suffix.hex}"
}

# Audio staging for the ASR fallback path -- deliberately separate bucket
# with a short lifecycle. Audio files are large, only needed transiently
# while a Transcribe job runs, and are the one artifact in this whole
# platform with genuine copyright sensitivity (a full audio track, not a
# derived text transcript) -- short TTL and tight IAM scoping both matter
# more here than on any other bucket in this project.
resource "aws_s3_bucket" "audio_staging" {
  bucket = "${var.project_name}-audio-staging-${random_id.suffix.hex}"
}

resource "aws_s3_bucket_lifecycle_configuration" "audio_staging" {
  bucket = aws_s3_bucket.audio_staging.id
  rule {
    id     = "expire-audio-fast"
    status = "Enabled"
    expiration {
      days = 2
    }
  }
}

locals {
  all_buckets = {
    landing        = aws_s3_bucket.landing
    cleansed       = aws_s3_bucket.cleansed
    analytics      = aws_s3_bucket.analytics
    athena_results = aws_s3_bucket.athena_results
    audio_staging  = aws_s3_bucket.audio_staging
  }
}

resource "aws_s3_bucket_public_access_block" "all" {
  for_each                = local.all_buckets
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "all" {
  for_each = local.all_buckets
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "landing" {
  bucket = aws_s3_bucket.landing.id
  versioning_configuration {
    status = "Enabled"
  }
}
