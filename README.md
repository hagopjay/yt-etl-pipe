# YouTube ETL Platform — with a human-in-the-loop transcript queue

Full implementation of the project brief's architecture (S3 medallion
data lake, Glue crawlers + Spark ETL, Lambda, Athena, QuickSight, IAM,
CloudWatch, Terraform-provisioned) **plus** a new subsystem: submit a
YouTube URL, get a transcript pulled automatically (captions first, ASR
fallback with confidence-gated human review second), persisted straight
into the same data lake so it's joinable against video statistics from
day one.

Read `docs/ARCHITECTURE_INSIGHTS.md` for the *why* (medallion rationale,
where this build knowingly deviates from the diagram and why, the
three-layer idempotency design, the proxy/ToS posture, the ASR
container-image decision, the human-review security model, cost model,
testing strategy, runbook, and honest scope boundaries). This README is
the *how to run it*.

## Repo map

```
terraform/     All AWS infrastructure — S3 medallion buckets, Glue
                (catalog, 2 crawlers, 2 Spark jobs), core Lambda (JSON->
                Parquet), the full transcript-queue subsystem (API
                Gateway, SQS FIFO, DynamoDB x2, Step Functions, Secrets
                Manager, EventBridge), CloudWatch alarms + dashboard,
                Athena workgroup
lambda/        Every Lambda function's source, plus the one Dockerfile
                for the container-image ASR-fallback function
glue/           2 PySpark ETL scripts (raw->Parquet, materialize-join)
stepfunctions/  The transcript pipeline's Amazon States Language
                definition (Terraform template)
frontend/       submit.html — single-file human submission form
athena/         DDL (partition projection) + analytical queries
quicksight/     boto3 provisioning script (Terraform's QuickSight
                support doesn't cover the whole setup — see docs §"why
                this is a script")
docs/           Architecture Insights masterclass doc
build.sh        Packages Lambda dependency bundles before `terraform
                apply` — REQUIRED first step, see below
```

## Prerequisites

- Terraform >= 1.7, an AWS account, S3 + DynamoDB backend for Terraform
  state (replace placeholders in `terraform/00_providers.tf`).
- Python 3.12 locally, Docker (for the one container-image Lambda),
  and an ECR repository to push it to.
- A **Webshare** (or equivalent rotating residential proxy) account —
  required for `fetch_captions` to work at all from Lambda; see
  `docs/ARCHITECTURE_INSIGHTS.md` §4.
- The Kaggle YouTube trending-videos dataset (CSV + per-region
  `{REGION}_category_id.json` files), uploaded to
  `landing/raw_statistics/` and `landing/category_reference/`
  respectively after the buckets exist.

## Run order

1. **Build Lambda dependency bundles**:
   ```
   ./build.sh
   ```
   This populates `lambda/build/submit_handler/` and
   `lambda/build/fetch_captions/` — Terraform's `archive_file` data
   sources zip whatever's in these directories, so this step must run
   (and re-run after any code change to those two functions) before
   every `terraform apply`.

2. **Build and push the ASR container image**:
   ```
   cd lambda
   docker build -f Dockerfile.start_transcribe_job -t youtube-etl-start-transcribe .
   # tag + push to your ECR repo — see the Dockerfile's header comment
   ```
   Pass the resulting image URI as `-var start_transcribe_job_image_uri=...`
   on `terraform apply` (or set it in a `.tfvars` file).

3. **Provision infrastructure**:
   ```
   cd terraform
   terraform init
   terraform apply
   ```
   Note the outputs — `api_base_url`, `landing_bucket`,
   `proxy_credentials_secret_arn`, `state_machine_arn`.

4. **Set the real proxy credentials** (Terraform deliberately only
   creates a placeholder — see `terraform/08_secrets.tf`):
   ```
   aws secretsmanager put-secret-value \
     --secret-id $(terraform output -raw proxy_credentials_secret_arn) \
     --secret-string '{"proxy_username":"...","proxy_password":"..."}'
   ```

5. **Upload the Glue scripts** to `s3://<landing_bucket>/scripts/` —
   `raw_to_parquet_job.py` and `materialize_categories_job.py` — matching
   the `script_location` values in `terraform/04_glue.tf`.

6. **Upload the dataset** to `landing/raw_statistics/{region}/` (CSV) and
   `landing/category_reference/` (JSON) to kick off the base pipeline.

7. **Point the frontend at your API**: edit `API_BASE` in
   `frontend/submit.html` to the `api_base_url` output, then open the
   file (or host it as a static S3 site) and submit a YouTube URL.

8. **Set up QuickSight** (after at least one successful
   `materialize_categories` Glue run):
   ```
   python quicksight/setup_quicksight.py \
     --account-id <your-account-id> \
     --workgroup <athena_workgroup output> \
     --database <analytics_glue_database output> \
     --quicksight-user-arn arn:aws:quicksight:...:user/default/<you>
   ```

## Feature flags worth knowing about (all in `terraform/01_variables.tf`)

- `enable_asr_fallback` (default `true`) — set `false` to make
  "no captions" a hard terminal state, skipping the container-image
  Lambda, Transcribe cost, and human-review path entirely.
- `respect_creator_disabled_captions` (default `true`) — see
  `docs/ARCHITECTURE_INSIGHTS.md` §5 for the product/ethics reasoning.
- `asr_confidence_review_threshold` (default `0.80`) — tune based on the
  Q3 query in `athena/queries.sql` once you have real ASR outcomes to
  look at.

## What to say in an interview about this project

`docs/ARCHITECTURE_INSIGHTS.md` §2 and §7 specifically: being able to
explain WHERE and WHY you deviated from a reference architecture (Lambda
vs. Glue Python Shell), and walking through a real security model for a
human-in-the-loop approval flow (task tokens, HMAC, single-use
enforcement) end-to-end, are the two things most likely to separate this
from a copy-pasted tutorial project in a technical interview.
