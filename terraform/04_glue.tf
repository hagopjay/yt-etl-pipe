############################################
# 04_glue.tf
#
# Two crawlers, not one -- raw_statistics (CSV, per-region trending
# video stats) and transcripts (JSON, from the new queue subsystem) have
# genuinely different schemas and arrival cadences. A single crawler
# across both prefixes would either merge incompatible schemas into one
# confusing table or silently create multiple tables anyway -- splitting
# them explicitly makes the catalog self-documenting.
############################################

resource "aws_glue_catalog_database" "raw" {
  name = "db_youtube_raw"
}

resource "aws_glue_catalog_database" "analytics" {
  name = "db_youtube_analytics"
}

resource "aws_glue_crawler" "raw_statistics" {
  name          = "${var.project_name}-raw-statistics-crawler"
  role          = aws_iam_role.glue.arn
  database_name = aws_glue_catalog_database.raw.name

  s3_target {
    path = "s3://${aws_s3_bucket.landing.bucket}/raw_statistics/"
  }

  schedule = "cron(0 */2 * * ? *)" # every 2h -- trending-video CSVs land daily, not worth hourly crawls

  schema_change_policy {
    update_behavior = "UPDATE_IN_DATABASE"
    delete_behavior = "LOG"
  }
}

resource "aws_glue_crawler" "transcripts" {
  name          = "${var.project_name}-transcripts-crawler"
  role          = aws_iam_role.glue.arn
  database_name = aws_glue_catalog_database.raw.name

  s3_target {
    path = "s3://${aws_s3_bucket.landing.bucket}/transcripts/"
  }

  # Human-submitted, arrives continuously and unpredictably -- runs far
  # more often than the CSV crawler, since a submitter reasonably expects
  # their transcript to become queryable within the hour, not the next
  # scheduled batch window.
  schedule = "cron(15,45 * * * ? *)"

  schema_change_policy {
    update_behavior = "UPDATE_IN_DATABASE"
    delete_behavior = "LOG"
  }
}

# --- Glue Studio Spark job #1: raw CSV -> Parquet (per project brief) ----
resource "aws_glue_job" "raw_to_parquet" {
  name         = "${var.project_name}-raw-to-parquet"
  role_arn     = aws_iam_role.glue.arn
  glue_version = "4.0"
  max_retries  = 1

  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.landing.bucket}/scripts/raw_to_parquet_job.py"
    python_version  = "3"
  }

  default_arguments = {
    "--job-bookmark-option"      = "job-bookmark-enable"
    "--enable-metrics"           = "true"
    "--enable-glue-datacatalog"  = "true"
    "--source_database"          = aws_glue_catalog_database.raw.name
    "--target_s3_path"           = "s3://${aws_s3_bucket.cleansed.bucket}/video_statistics/"
  }

  worker_type       = "G.1X"
  number_of_workers = 2
  timeout           = 30
}

# --- Glue Studio Spark job #2: materialize categories (join + curate) ---
resource "aws_glue_job" "materialize_categories" {
  name         = "bigdata-on-youtube-spark-materialize-categories"
  role_arn     = aws_iam_role.glue.arn
  glue_version = "4.0"
  max_retries  = 1

  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.landing.bucket}/scripts/materialize_categories_job.py"
    python_version  = "3"
  }

  default_arguments = {
    "--job-bookmark-option"      = "job-bookmark-enable"
    "--enable-metrics"           = "true"
    "--enable-glue-datacatalog"  = "true"
    "--cleansed_database"        = aws_glue_catalog_database.raw.name
    "--target_s3_path"           = "s3://${aws_s3_bucket.analytics.bucket}/materialized_categories/"
  }

  worker_type       = "G.1X"
  number_of_workers = 2
  timeout           = 30
}

resource "aws_glue_trigger" "materialize_after_raw" {
  name = "${var.project_name}-materialize-after-raw"
  type = "CONDITIONAL"

  predicate {
    conditions {
      job_name = aws_glue_job.raw_to_parquet.name
      state    = "SUCCEEDED"
    }
  }

  actions {
    job_name = aws_glue_job.materialize_categories.name
  }

  # ANALYTICS-layer freshness is DERIVED from the cleansed layer; chaining
  # via a conditional trigger (rather than two independent cron schedules)
  # means the join step can never run against a half-updated cleansed
  # table -- a scheduling race that's easy to introduce and annoying to
  # debug if these were both just independent cron jobs.
  start_on_creation = true
}
