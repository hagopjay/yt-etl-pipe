"""
raw_to_parquet_job.py — Glue 4.0 Spark job.

landing/raw_statistics/{region}/{region}videos.csv
    -> cleansed/video_statistics/ (Parquet, partitioned by region)

Per the project brief: "transform CSV files within the Glue Catalog
table into Parquet format data using Glue Studio." This is that
transform, expressed as a script (Glue Studio's visual editor generates
equivalent code to this -- writing it directly here keeps it reviewable
and diffable in version control, which the visual editor's generated
jobs are notoriously bad at).

INSIGHT -- why dedup by video_id + trending_date, not just video_id:
    This dataset tracks the SAME video across multiple days while it's
    trending -- a video can legitimately have many rows, one per day it
    appeared in the trending list, each with different view/like/comment
    counts as of that day. Deduping to one row per video_id would destroy
    the entire time-series signal the dataset exists to provide. The
    correct dedup key is the natural key of "one observation per video
    per trending day," and the correct dedup TARGET (when the same
    video+date pair appears more than once, e.g. from a re-run) is "keep
    the most recently ingested version," same idea as the accidents
    project's Glue job.
"""

import sys
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.context import SparkContext
from pyspark.sql import functions as F
from pyspark.sql.window import Window

args = getResolvedOptions(sys.argv, ["JOB_NAME", "source_database", "target_s3_path"])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

dyf = glueContext.create_dynamic_frame.from_catalog(
    database=args["source_database"],
    table_name="raw_statistics",
    transformation_ctx="raw_source",
)

df = dyf.toDF()

if df.rdd.isEmpty():
    print("No new records in this bookmark window — exiting cleanly.")
    job.commit()
    sys.exit(0)

df = (
    df.withColumn("views", F.col("views").cast("long"))
    .withColumn("likes", F.col("likes").cast("long"))
    .withColumn("dislikes", F.col("dislikes").cast("long"))
    .withColumn("comment_count", F.col("comment_count").cast("long"))
    .withColumn("category_id", F.col("category_id").cast("int"))
    .withColumn("trending_date_parsed", F.to_date("trending_date", "yy.dd.MM")) # Kaggle export's odd date format -- verify against the actual file header before relying on this
    .withColumn("publish_date_parsed", F.to_date(F.substring("publish_time", 1, 10), "yyyy-MM-dd"))
)

quality_mask = (
    F.col("video_id").isNotNull()
    & F.col("trending_date_parsed").isNotNull()
    & F.col("region").isNotNull()
)
clean = df.filter(quality_mask)
quarantined = df.filter(~quality_mask)

if quarantined.count() > 0:
    (
        quarantined.write.mode("append")
        .partitionBy("region")
        .parquet(args["target_s3_path"].rstrip("/") + "_quarantine/")
    )
    print(f"Quarantined {quarantined.count()} malformed rows this run.")

w = Window.partitionBy("video_id", "trending_date_parsed").orderBy(F.col("publish_date_parsed").desc())
deduped = (
    clean.withColumn("_rn", F.row_number().over(w))
    .filter(F.col("_rn") == 1)
    .drop("_rn")
)

(
    deduped.write.mode("append")
    .partitionBy("region")
    .parquet(args["target_s3_path"])
)

print(f"Wrote {deduped.count()} deduplicated video-statistics rows.")
job.commit()
