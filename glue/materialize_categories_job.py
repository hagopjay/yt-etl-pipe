"""
materialize_categories_job.py — Glue 4.0 Spark job.
Named `bigdata-on-youtube-spark-materialize-categories` in Terraform, per
the project brief's exact naming.

Joins:
    cleansed/video_statistics (from raw_to_parquet_job.py)
    cleansed/video_category_reference (from lambda/json_to_parquet.py)
    landing/transcripts (from the human-in-the-loop transcript subsystem)
on video_id/category_id/region, and writes ONE pre-joined,
QuickSight-ready table to the analytics layer.

INSIGHT -- this is the payoff for keeping transcripts in the same
    medallion pipeline (see persist_transcript.py's docstring): this join
    is a plain Spark left-join against a Glue Catalog table, not a
    cross-system API call or a separate ETL project. A video that has
    never been submitted for transcription simply gets nulls in the
    transcript columns -- exactly the right behavior for an optional,
    human-initiated enrichment.

INSIGHT -- why materialize this at all, instead of just letting Athena
    join the three tables ad-hoc at query time:
    QuickSight SPICE datasets and dashboard refresh performance benefit
    enormously from querying ONE flat, pre-joined table instead of
    re-executing a three-way join (one side of which -- video_statistics
    -- is the largest table in the whole project) on every dashboard
    load or SPICE refresh. This is the classic "pay the join cost once,
    on a schedule, instead of once per viewer per refresh" materialized-
    view trade-off, and it's exactly what the project brief's own
    "Data Materialization Using AWS Glue Studio" section describes.
"""

import sys
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.context import SparkContext
from pyspark.sql import functions as F

args = getResolvedOptions(sys.argv, ["JOB_NAME", "cleansed_database", "target_s3_path"])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

stats = glueContext.create_dynamic_frame.from_catalog(
    database=args["cleansed_database"], table_name="video_statistics", transformation_ctx="stats_source"
).toDF()

categories = glueContext.create_dynamic_frame.from_catalog(
    database=args["cleansed_database"], table_name="video_category_reference", transformation_ctx="categories_source"
).toDF()

# Transcripts table may not exist yet on a fresh deployment (no videos
# submitted through the queue yet) -- handle that gracefully rather than
# failing the whole materialization job over an optional enrichment
# source being empty.
try:
    transcripts = glueContext.create_dynamic_frame.from_catalog(
        database=args["cleansed_database"], table_name="transcripts", transformation_ctx="transcripts_source"
    ).toDF()
except Exception:
    print("No transcripts table found yet (no submissions processed) -- proceeding without transcript enrichment.")
    transcripts = spark.createDataFrame([], schema="video_id string, full_text string, transcript_source string, language string")

joined = (
    stats.join(categories, on=["category_id", "region"], how="left")
    .join(
        transcripts.select(
            "video_id",
            F.col("full_text").alias("transcript_text"),
            F.col("transcript_source"),
            F.col("language").alias("transcript_language"),
        ),
        on="video_id",
        how="left",
    )
    .withColumn("has_transcript", F.col("transcript_text").isNotNull())
    .withColumn("engagement_rate", F.when(F.col("views") > 0, (F.col("likes") + F.col("comment_count")) / F.col("views")).otherwise(F.lit(None)))
)

(
    joined.write.mode("overwrite") # this IS the materialized view -- full overwrite each run is correct, unlike the append-only raw/cleansed layers
    .partitionBy("region")
    .parquet(args["target_s3_path"])
)

print(f"Materialized {joined.count()} rows into the analytics layer ({joined.filter('has_transcript').count()} with a transcript).")
job.commit()
