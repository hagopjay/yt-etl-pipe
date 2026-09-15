"""
setup_quicksight.py — provisions the QuickSight data source + dataset
pointing at the materialized_categories Athena table.

INSIGHT -- why this is a script, not Terraform:
    Terraform's `aws_quicksight_*` resources exist but historically lag
    QuickSight's own feature surface (SPICE refresh schedules, certain
    dataset-permission shapes, embedding configuration) and several
    QuickSight setup steps (first-time account "sign up" / subscription,
    per-user role assignment) have NO Terraform resource at all and can
    only be done via the QuickSight console or a one-time boto3/CLI call.
    Rather than a partially-Terraform, partially-console setup that's
    confusing to follow, this project does ALL of QuickSight via one
    explicit, readable script -- consistent, if unconventional relative
    to "Terraform for everything." Know this trade-off; it's a real,
    common pattern for QuickSight specifically, not a shortcut taken only
    for this project.

Run once, after the Glue materialize_categories job has produced at
least one Parquet file (QuickSight's schema inference needs data to
introspect):

    python setup_quicksight.py --account-id 123456789012 --region us-east-1 \
        --workgroup youtube-etl-pipeline-workgroup --database db_youtube_analytics
"""

from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--account-id", required=True)
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--workgroup", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--table", default="materialized_categories")
    p.add_argument("--quicksight-user-arn", required=True,
                    help="ARN of the QuickSight user/group to grant owner permissions to, e.g. arn:aws:quicksight:us-east-1:ACCOUNT:user/default/USERNAME")
    args = p.parse_args()

    qs = boto3.client("quicksight", region_name=args.region)

    data_source_id = "youtube-etl-athena-source"
    dataset_id = "youtube-etl-materialized-categories"

    try:
        qs.create_data_source(
            AwsAccountId=args.account_id,
            DataSourceId=data_source_id,
            Name="YouTube ETL — Athena",
            Type="ATHENA",
            DataSourceParameters={"AthenaParameters": {"WorkGroup": args.workgroup}},
            Permissions=[{
                "Principal": args.quicksight_user_arn,
                "Actions": [
                    "quicksight:DescribeDataSource", "quicksight:DescribeDataSourcePermissions",
                    "quicksight:PassDataSource", "quicksight:UpdateDataSource",
                    "quicksight:DeleteDataSource", "quicksight:UpdateDataSourcePermissions",
                ],
            }],
        )
        print(f"Created data source {data_source_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            print(f"Data source {data_source_id} already exists — reusing.")
        else:
            raise

    try:
        qs.create_data_set(
            AwsAccountId=args.account_id,
            DataSetId=dataset_id,
            Name="YouTube Trending — Materialized Categories",
            PhysicalTableMap={
                "materializedCategoriesTable": {
                    "RelationalTable": {
                        "DataSourceArn": f"arn:aws:quicksight:{args.region}:{args.account_id}:datasource/{data_source_id}",
                        "Catalog": "AwsDataCatalog",
                        "Schema": args.database,
                        "Name": args.table,
                        "InputColumns": [
                            # QuickSight requires an explicit column list;
                            # this mirrors athena/queries.sql's DDL.
                            {"Name": "video_id", "Type": "STRING"},
                            {"Name": "trending_date_parsed", "Type": "DATETIME"},
                            {"Name": "title", "Type": "STRING"},
                            {"Name": "channel_title", "Type": "STRING"},
                            {"Name": "views", "Type": "INTEGER"},
                            {"Name": "likes", "Type": "INTEGER"},
                            {"Name": "dislikes", "Type": "INTEGER"},
                            {"Name": "comment_count", "Type": "INTEGER"},
                            {"Name": "category_name", "Type": "STRING"},
                            {"Name": "transcript_source", "Type": "STRING"},
                            {"Name": "has_transcript", "Type": "STRING"},  # QuickSight has no native BOOLEAN input column type -- cast in a calculated field instead
                            {"Name": "engagement_rate", "Type": "DECIMAL"},
                            {"Name": "region", "Type": "STRING"},
                        ],
                    }
                }
            },
            ImportMode="SPICE",  # SPICE (in-memory) over direct-query: dashboard interactivity matters more here than sub-minute freshness, and the materialize job already runs on its own schedule
            Permissions=[{
                "Principal": args.quicksight_user_arn,
                "Actions": [
                    "quicksight:DescribeDataSet", "quicksight:DescribeDataSetPermissions",
                    "quicksight:PassDataSet", "quicksight:DescribeIngestion", "quicksight:ListIngestions",
                    "quicksight:UpdateDataSet", "quicksight:DeleteDataSet", "quicksight:CreateIngestion",
                    "quicksight:CancelIngestion", "quicksight:UpdateDataSetPermissions",
                ],
            }],
        )
        print(f"Created dataset {dataset_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            print(f"Dataset {dataset_id} already exists — reusing.")
        else:
            raise

    print(
        "\nNext step (console, no API for this part): open QuickSight, "
        "create a new Analysis from this dataset, and build your dashboard. "
        "Schedule a SPICE refresh to run shortly after the "
        "bigdata-on-youtube-spark-materialize-categories Glue job's own schedule."
    )


if __name__ == "__main__":
    main()
