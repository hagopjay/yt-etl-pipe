"""Shared test setup: put lambda/ and ingest/ on sys.path and give every
Lambda module the environment variables it reads at import time."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("JOBS_TABLE", "jobs")
os.environ.setdefault("QUEUE_URL", "placeholder")
os.environ.setdefault("STATE_MACHINE_ARN", "placeholder")
os.environ.setdefault("PROXY_CREDENTIALS_SECRET_ARN", "placeholder")
os.environ.setdefault("LANDING_BUCKET", "landing")
