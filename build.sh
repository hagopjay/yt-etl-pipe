#!/usr/bin/env bash
# build.sh — populates lambda/build/{submit_handler,fetch_captions}/
# with each function's code + third-party dependencies, ready for
# terraform/09_lambda_transcript.tf's archive_file data sources to zip.
#
# Run this BEFORE `terraform apply`, and re-run it after any change to
# submit_handler.py, youtube_url.py, fetch_captions.py, or their
# requirements files.
set -euo pipefail

cd "$(dirname "$0")/lambda"

rm -rf build
mkdir -p build/submit_handler build/fetch_captions

# submit_handler has no third-party deps, but DOES depend on the shared
# youtube_url.py module living alongside it in the same zip (Lambda zips
# are flat -- there's no package-relative import across separate
# function zips, so shared code gets copied into each zip that needs it).
cp submit_handler.py youtube_url.py build/submit_handler/

pip install -r requirements-fetch-captions.txt -t build/fetch_captions/ --no-cache-dir --quiet
cp fetch_captions.py build/fetch_captions/

echo "Build complete. build/submit_handler and build/fetch_captions are ready for terraform apply."
echo "Remember: start_transcribe_job is a container image -- build/push it separately per lambda/Dockerfile.start_transcribe_job."
