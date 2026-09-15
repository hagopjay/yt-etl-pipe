"""
json_to_parquet.py — base project Lambda.

Triggered by S3 ObjectCreated on landing/category_reference/*.json (the
per-region `US_category_id.json`-style files in the Kaggle YouTube
dataset: a category_id -> category name mapping that differs by region).

Converts JSON -> Parquet and registers/updates the Glue Catalog table in
one call via AWS Data Wrangler (awswrangler), per the project brief's
explicit instruction to use Data Wrangler for this step.

INSIGHT -- why Data Wrangler instead of hand-rolled boto3 + pyarrow:
    wr.s3.to_parquet(..., dataset=True, database=..., table=...) does
    THREE things atomically that you'd otherwise write yourself: writes
    the Parquet file(s) to S3, creates-or-updates the Glue Catalog table
    definition to match the DataFrame's inferred schema, and (with
    mode="append") adds any new partitions. For a Lambda-sized job (single
    small JSON file per invocation) this collapses what would be a
    50+ line boto3 Glue API dance into one call -- exactly the kind of
    leverage that justifies pulling in a fairly heavy dependency (pandas +
    pyarrow) for a function that otherwise does very little.

INSIGHT -- why this run as Lambda at all, rather than in the Glue Spark
    jobs that already exist in this project:
    Category-reference files are tiny (a few KB, one file per region) and
    arrive irregularly (whenever someone adds a new region export) --
    spinning up a Glue job (worker warm-up alone is often 30-60s) for a
    sub-second amount of actual work is wasteful. Lambda's per-request
    billing and near-zero cold-start-to-first-byte time fits this
    "small, frequent, bursty" shape; the Glue Spark jobs elsewhere in
    this project fit the "large, scheduled, batch" shape of the actual
    video-statistics CSVs. Picking compute per job SHAPE, not per project
    convention, is the general principle.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse

import awswrangler as wr
import boto3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("json_to_parquet")

s3 = boto3.client("s3")

CLEANSED_BUCKET = os.environ["CLEANSED_BUCKET"]
GLUE_DATABASE = os.environ["GLUE_DATABASE"]
GLUE_TABLE = os.environ["GLUE_TABLE"]


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        log.info("Processing s3://%s/%s", bucket, key)

        try:
            results.append(process_one(bucket, key))
        except Exception:
            # A single malformed category file must not fail the whole
            # batch (S3 -> Lambda invocations here are effectively
            # single-record, but keep this shape defensive in case the
            # trigger is ever changed to a batching source).
            log.exception("Failed to process s3://%s/%s", bucket, key)
            results.append({"key": key, "status": "FAILED"})

    return {"results": results}


def process_one(bucket: str, key: str) -> dict:
    obj = s3.get_object(Bucket=bucket, Key=key)
    payload = json.loads(obj["Body"].read())

    # The Kaggle export nests categories under "items"; each item's
    # id + snippet.title is the (category_id, category_name) pair every
    # downstream join in this project needs.
    region = _region_from_key(key)
    rows = [
        {
            "category_id": int(item["id"]),
            "category_name": item["snippet"]["title"],
            "region": region,
        }
        for item in payload.get("items", [])
    ]

    if not rows:
        log.warning("No category items found in %s -- skipping write.", key)
        return {"key": key, "status": "EMPTY"}

    import pandas as pd  # local import: keep Lambda cold start light for invocations that error before needing pandas at all

    df = pd.DataFrame(rows)

    wr.s3.to_parquet(
        df=df,
        path=f"s3://{CLEANSED_BUCKET}/video_category_reference/",
        dataset=True,
        database=GLUE_DATABASE,
        table=GLUE_TABLE,
        mode="append",
        partition_cols=["region"],
        # Overwrite-by-partition semantics would be wrong here: category
        # references are near-static reference data, and "append" plus
        # a downstream dedup-on-read (or a periodic compaction job) is
        # simpler than trying to make this Lambda transactionally
        # replace a partition on every tiny update.
    )

    return {"key": key, "status": "OK", "rows_written": len(rows), "region": region}


def _region_from_key(key: str) -> str:
    """Kaggle filenames follow the `{REGION}_category_id.json` convention,
    e.g. `US_category_id.json`, `GB_category_id.json`."""
    filename = key.rsplit("/", 1)[-1]
    return filename.split("_", 1)[0].upper() if "_" in filename else "UNKNOWN"
