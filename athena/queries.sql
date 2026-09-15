-- athena/queries.sql
--
-- Table definitions use partition projection (same pattern as the
-- accidents-platform project) since the writer is always one of this
-- project's own Glue jobs and the partition scheme is therefore known
-- up front -- no crawler needed on the analytics layer itself (the raw
-- layer's crawlers in terraform/04_glue.tf are a separate, deliberate
-- exception: raw JSON/CSV schema genuinely benefits from discovery).

CREATE EXTERNAL TABLE IF NOT EXISTS materialized_categories (
    video_id           string,
    trending_date_parsed date,
    publish_date_parsed  date,
    title              string,
    channel_title      string,
    views              bigint,
    likes              bigint,
    dislikes           bigint,
    comment_count      bigint,
    category_id        int,
    category_name      string,
    transcript_text    string,
    transcript_source  string,
    transcript_language string,
    has_transcript     boolean,
    engagement_rate    double
)
PARTITIONED BY (region string)
STORED AS PARQUET
LOCATION 's3://REPLACE-ME-analytics-bucket/materialized_categories/'
TBLPROPERTIES (
    'projection.enabled' = 'true',
    'projection.region.type'   = 'enum',
    'projection.region.values' = 'US,GB,CA,DE,FR,IN,JP,KR,MX,RU',
    'storage.location.template' = 's3://REPLACE-ME-analytics-bucket/materialized_categories/region=${region}/'
);

CREATE EXTERNAL TABLE IF NOT EXISTS transcripts (
    video_id          string,
    language          string,
    transcript_source string,
    is_generated      boolean,
    asr_avg_confidence double,
    reviewed_by_human boolean,
    full_text         string,
    persisted_at      string
)
PARTITIONED BY (dt string)
STORED AS PARQUET  -- landing layer is JSON via the crawler; this definition targets the CLEANSED/analytics copy after a Glue transform, if one is added -- see docs for the JSON-vs-Parquet note on the raw transcripts table
LOCATION 's3://REPLACE-ME-cleansed-bucket/transcripts/'
TBLPROPERTIES (
    'projection.enabled' = 'true',
    'projection.dt.type'          = 'date',
    'projection.dt.range'         = '2024-01-01,NOW',
    'projection.dt.format'        = 'yyyy-MM-dd',
    'storage.location.template'   = 's3://REPLACE-ME-cleansed-bucket/transcripts/dt=${dt}/'
);

-- ============================================================
-- Q1: Which categories have the highest average engagement rate,
--     and how many of their trending videos have a transcript on file?
-- ============================================================
SELECT
    category_name,
    COUNT(*) AS trending_appearances,
    AVG(engagement_rate) AS avg_engagement_rate,
    SUM(CASE WHEN has_transcript THEN 1 ELSE 0 END) AS with_transcript
FROM materialized_categories
WHERE region = 'US'
GROUP BY category_name
ORDER BY avg_engagement_rate DESC
LIMIT 20;

-- ============================================================
-- Q2: Full-text search over transcripts for a keyword, joined back to
--     view counts -- "how did videos that mention X perform".
-- ============================================================
SELECT
    video_id,
    title,
    views,
    engagement_rate,
    transcript_source
FROM materialized_categories
WHERE has_transcript
  AND lower(transcript_text) LIKE '%artificial intelligence%'
ORDER BY views DESC
LIMIT 25;

-- ============================================================
-- Q3: ASR quality audit -- distribution of confidence scores for
--     ASR-sourced transcripts, to sanity-check the
--     asr_confidence_review_threshold setting against real outcomes.
-- ============================================================
SELECT
    reviewed_by_human,
    CASE
        WHEN asr_avg_confidence >= 0.9 THEN '0.90-1.00'
        WHEN asr_avg_confidence >= 0.8 THEN '0.80-0.89'
        WHEN asr_avg_confidence >= 0.7 THEN '0.70-0.79'
        ELSE 'below 0.70'
    END AS confidence_bucket,
    COUNT(*) AS transcript_count
FROM transcripts
WHERE transcript_source = 'asr'
GROUP BY reviewed_by_human,
    CASE
        WHEN asr_avg_confidence >= 0.9 THEN '0.90-1.00'
        WHEN asr_avg_confidence >= 0.8 THEN '0.80-0.89'
        WHEN asr_avg_confidence >= 0.7 THEN '0.70-0.79'
        ELSE 'below 0.70'
    END
ORDER BY confidence_bucket;

-- ============================================================
-- Q4: Submission funnel -- how many videos land in each terminal
--     status, to see where the pipeline is losing videos (useful for
--     tuning enable_asr_fallback / respect_creator_disabled_captions).
--     Run against the DynamoDB jobs table export or a periodic Glue
--     job that snapshots it to S3 -- Athena has no native DynamoDB
--     connector without setting up the Athena Federated Query Lambda
--     connector, noted here rather than wired up to keep this project's
--     infrastructure surface bounded.
-- ============================================================
-- SELECT status, COUNT(*) FROM jobs_table_snapshot GROUP BY status;
