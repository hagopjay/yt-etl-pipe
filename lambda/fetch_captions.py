"""
fetch_captions.py — Step Functions Task, first real work in the
transcript pipeline.

================================================================================
WHY THIS FUNCTION EXISTS THE WAY IT DOES (read before changing error
handling below -- every branch here maps to a specific, verified 2026
failure mode of youtube-transcript-api running from AWS)
================================================================================

1. A PROXY IS NOT OPTIONAL FROM LAMBDA.
   YouTube's caption endpoints reliably block requests from cloud-
   provider IP ranges (AWS/GCP/Azure datacenter IPs), independent of
   request rate -- this is IP-reputation-based blocking, not simple rate
   limiting, and it affects a Lambda's very first request, not just
   request #1000. The maintained fix is a ROTATING RESIDENTIAL proxy
   pool (Webshare is the library's own first-class integration via
   `WebshareProxyConfig`), not a static datacenter proxy -- datacenter
   proxy IPs get blocked too. This is why terraform/08_secrets.tf exists
   and why this function refuses to run without proxy credentials
   configured, rather than silently falling back to a direct connection
   that will just fail.

2. "NO TRANSCRIPT" IS THREE DIFFERENT THINGS, NOT ONE.
   - TranscriptsDisabled: the CREATOR turned captions off for this video.
     A deliberate signal, arguably one we should respect (see
     `respect_creator_disabled_captions` in terraform/01_variables.tf).
   - NoTranscriptFound: captions exist in some language, just not one
     this call asked for -- worth retrying with a broader language list
     before giving up.
   - VideoUnavailable: the video is private, deleted, or geo-restricted.
     Nothing downstream (proxy, ASR, human review) can fix this.
   Collapsing these into one "failed" bucket, which a LOT of example code
   does, throws away exactly the information the Step Functions Choice
   state needs to route correctly.

3. BLOCKED IS RETRYABLE; DISABLED AND UNAVAILABLE ARE NOT.
   RequestBlocked / IpBlocked / TooManyRequests / the newer PoTokenRequired
   error class are all transient-ish infrastructure failures -- a proxy
   rotation or a backoff window can genuinely fix them on the next
   attempt. This function raises a DISTINCT exception type for these so
   Step Functions' native Retry (exponential backoff, see the ASL
   definition) only fires for the failure mode retrying can actually fix.

4. LEGAL/ToS POSTURE, STATED PLAINLY:
   youtube-transcript-api accesses an undocumented, non-authenticated
   caption endpoint. YouTube's Terms of Service prohibit automated
   access "through any automated means" without permission; this sits in
   a widely-used but genuinely gray area -- low-volume personal/research
   use is broadly tolerated in practice, high-volume commercial use
   carries more real risk (both of being blocked harder and, separately,
   of a ToS claim). This project is built assuming personal/research-
   scale, human-submitted usage. Scaling this past that assumption is a
   business decision to make with actual legal counsel, not a config
   flag to flip.
================================================================================
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    RequestBlocked,
    IpBlocked,
    CouldNotRetrieveTranscript,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fetch_captions")

SECRET_ARN = os.environ["PROXY_CREDENTIALS_SECRET_ARN"]
PREFERRED_LANGUAGES = ["en", "en-US", "en-GB"]  # broadened to "all available" on NoTranscriptFound retry -- see below

_secrets_client = boto3.client("secretsmanager")
_cached_api_client = None  # module-level cache: reused across warm Lambda invocations, avoiding a Secrets Manager call (and a new proxy session) on every request


class BlockedRetryable(Exception):
    """Raised for failure modes a retry (with backoff / proxy rotation on
    the NEXT attempt) can plausibly fix. Caught specifically by Step
    Functions' Retry config in the ASL definition -- NOT the same as a
    generic Exception, which Step Functions would also retry by default
    for the wrong reasons (e.g. a code bug)."""


def _get_client() -> YouTubeTranscriptApi:
    global _cached_api_client
    if _cached_api_client is not None:
        return _cached_api_client

    secret = json.loads(_secrets_client.get_secret_value(SecretId=SECRET_ARN)["SecretString"])
    if not secret.get("proxy_username") or secret["proxy_username"].startswith("REPLACE"):
        raise RuntimeError(
            "Proxy credentials are still the Terraform placeholder. "
            "Set the real Webshare credentials via `aws secretsmanager put-secret-value` "
            "before this function can reach YouTube from Lambda -- see docs/ARCHITECTURE_INSIGHTS.md."
        )

    _cached_api_client = YouTubeTranscriptApi(
        proxy_config=WebshareProxyConfig(
            proxy_username=secret["proxy_username"],
            proxy_password=secret["proxy_password"],
            # Retries INSIDE the proxy layer for a fresh IP from the pool,
            # separate from and faster than Step Functions' own retry --
            # cheap first line of defense before escalating to a full
            # state-machine-level backoff.
            retries_when_blocked=3,
        )
    )
    return _cached_api_client


def handler(event, context):
    video_id = event["video_id"]
    api = _get_client()

    # INSIGHT -- every branch below returns the SAME set of keys, just
    # with nulls where a field doesn't apply. Step Functions' Parameters
    # intrinsic (`"field.$": "$.some.path"`) throws a hard runtime error
    # if the referenced path doesn't exist in the input -- so the
    # downstream state machine's reshape step (see "MergeFetchResult" in
    # the ASL) can only safely reference `$.language`, `$.segments`, etc.
    # unconditionally if THIS function guarantees they're always present.
    # Designing a Lambda's output shape around what the STATE MACHINE
    # needs to safely reference, not just what's convenient to return, is
    # a real and easy-to-miss Step Functions integration discipline.
    _empty_transcript_fields = {"language": None, "segments": [], "is_generated": None, "transcript_source": None}

    try:
        transcript = _fetch_with_language_fallback(api, video_id)
    except TranscriptsDisabled:
        log.info("Captions explicitly disabled by creator for video_id=%s", video_id)
        return {"status": "DISABLED", "video_id": video_id, **_empty_transcript_fields}
    except VideoUnavailable:
        log.info("Video unavailable (private/deleted/geo-blocked) for video_id=%s", video_id)
        return {"status": "UNAVAILABLE", "video_id": video_id, **_empty_transcript_fields}
    except NoTranscriptFound:
        log.info("No transcript in any available language for video_id=%s", video_id)
        return {"status": "NO_CAPTIONS", "video_id": video_id, **_empty_transcript_fields}
    except (RequestBlocked, IpBlocked) as e:
        log.warning("Blocked fetching video_id=%s: %s -- flagging as retryable.", video_id, e)
        raise BlockedRetryable(str(e)) from e
    except CouldNotRetrieveTranscript as e:
        # Catch-all for the library's other transient-ish errors
        # (including newer bot-detection classes like PoTokenRequired,
        # which library support is still evolving for as of this
        # writing) -- treat conservatively as retryable rather than
        # silently swallowing an error class we don't have a specific
        # branch for yet.
        log.warning("Transient retrieval failure for video_id=%s: %s", video_id, e)
        raise BlockedRetryable(str(e)) from e

    segments = [
        {"start": s.start, "duration": s.duration, "text": s.text}
        for s in transcript.snippets
    ]

    return {
        "status": "SUCCESS",
        "video_id": video_id,
        "language": transcript.language_code,
        "is_generated": transcript.is_generated,  # auto-ASR-by-YouTube vs. manually authored -- a real quality signal downstream
        "segments": segments,
        "transcript_source": "caption_auto" if transcript.is_generated else "caption_manual",
    }


def _fetch_with_language_fallback(api: YouTubeTranscriptApi, video_id: str):
    """Try the preferred (English) languages first; on NoTranscriptFound,
    fall back to whatever the video actually has rather than giving up --
    a French or Hindi transcript is far more useful downstream than no
    transcript at all, and the language is recorded on the result either
    way so nothing is silently mislabeled as English."""
    try:
        return api.fetch(video_id, languages=PREFERRED_LANGUAGES)
    except NoTranscriptFound:
        transcript_list = api.list(video_id)
        # .list() raises TranscriptsDisabled/VideoUnavailable itself if
        # applicable, which propagates up to the caller's except blocks.
        first_available = next(iter(transcript_list))
        return first_available.fetch()
