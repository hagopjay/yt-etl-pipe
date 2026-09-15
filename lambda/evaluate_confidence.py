"""
evaluate_confidence.py — Step Functions Task (plain synchronous
invocation, no task token). A deliberately tiny, pure function: one
threshold comparison, no I/O. Kept as its own Lambda rather than a Step
Functions Choice state directly on the ASR output because the threshold
is an environment variable sourced from Terraform (`asr_confidence_review_threshold`),
and a Lambda is the natural place for "config-driven business logic" to
live versus baking the number into the state machine definition itself.

Only ever invoked on the ASR branch -- caption-sourced transcripts (manual
or YouTube's own auto-captions) skip this step entirely and go straight
to persist_transcript.py, because a human already reviewed manual
captions by definition, and YouTube's own auto-captions have no
comparable per-item confidence score exposed to build a threshold on.
"""

import os

CONFIDENCE_THRESHOLD = float(os.environ.get("ASR_CONFIDENCE_THRESHOLD", "0.80"))


def handler(event, context):
    avg_confidence = event.get("asr_avg_confidence", 0.0)
    needs_review = avg_confidence < CONFIDENCE_THRESHOLD

    return {
        **event,
        "needs_review": needs_review,
        "confidence_threshold_used": CONFIDENCE_THRESHOLD,
    }
