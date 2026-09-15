# YouTube ETL Platform — Architecture Insights

Companion to `README.md` (run order) and the inline comments in every
file (local decisions). This covers the decisions that span files —
read it once, then use it as a reference when a comment says "see docs."

---

## 1. Why transcripts live in the SAME medallion pipeline, not a side project

The brief's diagram shows one Data Lake (Landing / Cleansed-Enriched /
Analytics-Reporting) fed by one Data Processing layer (Glue + Lambda).
The transcript-queue subsystem is designed to be a **new source feeding
the same lake**, not a parallel system:

- `request_human_review.py` and `persist_transcript.py` write to the
  same `landing` bucket the CSV/JSON statistics land in, just under a
  different prefix (`transcripts/` vs `raw_statistics/`).
- A dedicated Glue crawler (`transcripts`, running far more often than
  the statistics crawler — see `terraform/04_glue.tf`) catalogs it into
  the same Glue database.
- `materialize_categories_job.py` LEFT JOINs it into the existing
  analytics table on `video_id`. A video nobody has ever submitted for
  transcription just gets nulls in three columns — no special-casing
  needed anywhere downstream (Athena, QuickSight) for "does this video
  have a transcript."

This is the single highest-leverage design decision in this build: it
means "ultra advanced" didn't have to mean "a second, disconnected
system bolted onto the first."

---

## 2. Where this build deliberately DIFFERS from the diagram, and why

The diagram shows **Glue Python Shell** for the general Data Processing
box, alongside Lambda. This build uses **Lambda** for the JSON
category-reference transform (`lambda/json_to_parquet.py`) instead of a
Glue Python Shell job. That's a real substitution, stated plainly:

Glue Python Shell jobs are billed per-DPU-hour with a minimum
(historically 1-minute) billing granularity — fine for a job that runs
on a SCHEDULE over accumulated files, wasteful for a job triggered by a
single small S3 PUT event, which is this transform's actual trigger
pattern. Lambda's sub-second, per-invocation billing fits "small,
frequent, bursty" better; Python Shell (or the full Spark jobs this
project DOES use) fit "large, scheduled, batch" better. **Pick compute
by the job's shape, not by matching every box in a reference diagram
literally** — and say so explicitly when you deviate, the way this
paragraph just did, rather than let a reviewer assume you missed the
diagram's intent.

---

## 3. The transcript queue's three independent idempotency layers

Submitting the same video twice (a double-click, a retried request, a
redelivered SQS message) must not produce two competing Step Functions
executions, two proxy-fetch attempts, or two Transcribe jobs. Three
layers, each covering a failure mode the others don't:

1. **DynamoDB conditional put** (`submit_handler.py`) — rejects a
   second submission before it ever reaches SQS, and returns a cached
   result instantly for an already-`COMPLETED` video. Cheapest layer;
   catches the overwhelming majority of real-world duplicates (a user
   clicking submit twice).
2. **SQS FIFO `MessageGroupId` + `ContentBasedDeduplication`**
   (`terraform/07_sqs_sns.tf`) — serializes same-video messages so two
   consumers can never process the same video concurrently, and
   collapses exact-duplicate message bodies within a 5-minute window.
3. **Deterministic Step Functions execution name**
   (`start_workflow.py`, `vid-{video_id}`) — the last line of defense:
   even if the same message were somehow delivered twice (SQS's FIFO
   guarantee is exactly-once-ish under NORMAL operation, but AWS's own
   documentation is careful not to promise it under all failure
   conditions), `StartExecution` on an existing name fails with
   `ExecutionAlreadyExists` instead of silently starting a duplicate.

Three layers for one property looks like over-engineering in a demo.
It's the minimum for production, because each layer's blind spot is a
different, real failure mode.

---

## 4. Why a residential proxy is load-bearing, not a nice-to-have

`youtube_transcript_api` talks to an undocumented, unauthenticated
YouTube caption endpoint. As of this writing, requests originating from
AWS/GCP/Azure datacenter IP ranges are reliably blocked — this is
IP-reputation-based, not simple rate-limiting, so it affects a Lambda's
FIRST request, not just its thousandth. The maintained fix, and the
library's own first-class integration, is a rotating RESIDENTIAL proxy
pool (Webshare); a static datacenter proxy doesn't fix this, since
datacenter IP ranges are exactly what's blocked. This is why
`fetch_captions.py` refuses to run against placeholder credentials
rather than silently attempting (and failing) a direct connection, and
why `reserved_concurrent_executions = 3` on that Lambda is a deliberate
throttle: uncapped concurrency would burn through the proxy pool's rate
budget in one burst and turn a healthy pool into a self-inflicted outage.

**Legal/ToS posture, stated plainly (not a lawyer, not legal advice):**
YouTube's Terms of Service prohibit automated access "through any
automated means" without permission. This is a widely-used but genuinely
gray area in practice — low-volume, personal/research-scale, human-
submitted usage (what this project assumes) is broadly tolerated;
scaling this to high-volume commercial use is a materially different
risk profile, both for harder blocking and for a real ToS claim. That's
a business decision to make with actual counsel, not a config flag.

---

## 5. Three different "no transcript" outcomes, and why they're not one

- **`TranscriptsDisabled`** — the creator turned captions off. A
  deliberate signal; `respect_creator_disabled_captions=true` (default)
  means this is terminal and never falls through to ASR, on the theory
  that overriding a creator's explicit choice with a synthesized
  transcript is a product/ethics call this project defaults toward NOT
  making silently. Flip the variable to override that default.
- **`NoTranscriptFound`** — captions exist in some language, just not
  the ones requested. `fetch_captions.py` retries with whatever language
  IS available before giving up — a French transcript is far more
  useful downstream than nothing, and the language is always recorded.
- **`VideoUnavailable`** — private, deleted, or geo-restricted. Nothing
  downstream (proxy rotation, ASR, human review) can fix this; it's
  terminal immediately.

A lot of example code collapses all three into one "failed" bucket. That
throws away exactly the information the state machine's `Choice` states
need to route correctly — this is the caption-fetch equivalent of the
accidents-platform project's "IteratorAge tells you something count
metrics don't" lesson.

---

## 6. The ASR fallback path, and why it's the one container-image Lambda

`start_transcribe_job.py` needs `yt-dlp` (Python package, fine in a zip)
AND a static `ffmpeg` binary (70-80MB, not fine in a standard zip +
layer without real friction). Every other function in this project is a
plain zip; this ONE is a container image (see
`lambda/Dockerfile.start_transcribe_job`) — a deliberate, isolated
exception rather than moving the whole project to container images,
which would add build/push overhead to every function for a need only
one of them has.

**Why `.waitForTaskToken` instead of polling inside the Lambda**:
Transcribe jobs run anywhere from under a minute to several minutes.
Blocking a Lambda invocation on a poll loop for that long burns
duration-based billing on pure waiting and risks the function timeout on
longer videos. Returning immediately after `start_transcription_job` and
letting an EventBridge rule on Transcribe's own job-state-change event
(`transcribe_callback.py`) resume the execution means **zero compute
spent waiting**, regardless of audio length.

**Recovering context without a second lookup table**: the Transcribe job
name is constructed as `transcript-{video_id}-{8hexchars}` specifically
so `transcribe_callback.py` can parse `video_id` back out of the
EventBridge event (which carries the job name but no custom metadata) by
splitting off the last hyphen segment — safe even though YouTube video
IDs may themselves contain hyphens, because the 8-hex-character suffix
never does.

---

## 7. Confidence-gated human review — the security model, consolidated

The Step Functions task token is a bearer credential: whoever presents
it resumes (or fails) that specific paused execution. It is NEVER placed
directly in an emailed link — email is not a secure channel (forwarding,
shared inboxes, mail-client link-preview crawlers that pre-fetch every
URL in a message, a real and common failure mode for exactly this kind
of approval flow). Four layers instead:

1. An opaque, unguessable `review_id` (UUID) is what appears in the
   email; the real task token lives only in DynamoDB, addressed by it.
2. An HMAC-SHA256 signature over `(review_id, decision)`, keyed by a
   Terraform-generated secret, stops someone from taking a legitimate
   "reject" link and editing the query string to "approve."
3. Single-use enforcement via a **conditional** DynamoDB update
   (`PENDING` → `CONSUMED`) — the first request wins; a link-preview
   crawler's pre-fetch gets "already handled," and so does the human's
   real click if the crawler won the race. (A production version would
   use a confirm-then-POST pattern instead of a link-triggering GET to
   close this gap properly — noted, not implemented, to keep this
   project's frontend surface to the one plain HTML form.)
4. A DynamoDB TTL (`review_token_ttl_hours`, default 72h) auto-expires
   stale reviews; Step Functions' own `TimeoutSeconds` on the
   `RequestHumanReview` state fails the execution safely (to
   `REVIEW_EXPIRED`, never left hanging) if nobody ever responds.

**Only videos that need it hit this gate.** Manually-authored captions
and YouTube's own auto-captions skip straight to publish — a human
already made the caption, or YouTube's own system did with no comparable
per-item confidence score to threshold on. Only this project's OWN ASR
fallback, which does expose per-word confidence, gets evaluated and
conditionally gated. Gating every video would defeat the entire point of
automating the common case.

---

## 8. Cost model (order-of-magnitude, `us-east-1`, verify current pricing)

| Component | Cost driver | Note |
|---|---|---|
| Lambda (all zip functions) | Near $0 at demo volume | Sub-second billing, low invocation count |
| `start_transcribe_job` (container image) | Slightly higher cold-start + a small ECR storage cost | Still per-invocation billed; the container format is a packaging choice, not a pricing-model change |
| SQS FIFO | ~$0.50/million requests | FIFO costs the same per-request as Standard; the ordering/dedup guarantee is free, you're only paying for volume |
| Step Functions (Standard) | $0.025 per 1,000 state transitions | A 20-state definition with retries can rack up transitions faster than it looks — Express workflows are cheaper per-transition but cap execution duration at 5 minutes, incompatible with a multi-hour human-review wait |
| DynamoDB (both tables) | Near $0 | On-demand billing, low item count and size at this scale |
| AWS Transcribe | ~$0.024/minute of audio (standard batch) | **The single largest per-submission cost** if ASR fallback fires often — a 10-minute video costs ~$0.24 to transcribe; budget accordingly if `enable_asr_fallback` sees heavy use |
| S3 (all buckets) | Near $0 | Small dataset; audio-staging's 2-day lifecycle keeps the one large-file bucket from accumulating |
| Glue crawlers + Spark jobs | ~$15-30/month | Same right-sizing logic as the accidents-platform project — 2×G.1X workers, short timeouts |
| Athena | Near $0 if partition-pruned; guarded by the workgroup's 5GB scan cutoff | Same FinOps guardrail as before |
| QuickSight | **Pay-per-session**, not per-seat — genuinely cost-effective for occasional dashboard viewers, since you only pay when someone opens it | The brief's own description of QuickSight's pricing model is accurate and worth knowing cold for an interview |
| CloudWatch Logs/alarms/dashboard | A few dollars/month | Standard log retention + alarm evaluation costs |

**The single highest-leverage cost insight**: Transcribe is the one
component whose cost scales with CONTENT LENGTH, not request count or
compute time. A pipeline that looks cheap in testing (short demo videos)
can surprise you in production with long-form content (podcasts,
lectures) unless `enable_asr_fallback` and the confidence threshold are
tuned with that in mind.

---

## 9. Security hardening beyond the project defaults

Beyond the scoped IAM, SSE-KMS on every bucket/SNS topic/Kinesis-
adjacent resource, and `block_public_*` everywhere already in the
Terraform:

- **VPC endpoints** for S3/DynamoDB/Secrets Manager/Transcribe so
  Lambda-to-AWS-service traffic never needs a NAT gateway or the public
  internet (the ONE exception is `fetch_captions`'s and
  `start_transcribe_job`'s outbound calls to YouTube itself, which by
  definition must leave the VPC through the proxy).
- **A GuardDuty finding to watch for specifically**: this project's
  normal behavior (a Lambda making frequent outbound HTTPS calls to a
  changing set of external IPs, mediated through a proxy) can resemble
  the network pattern GuardDuty flags for compromised-instance C2
  beaconing. Know this ahead of time so a real finding doesn't get
  reflexively dismissed as "oh that's just the proxy" without checking.
- **Customer-managed KMS keys** instead of the AWS-managed ones used
  here for brevity, if this ever needs to satisfy a compliance
  requirement around key rotation/cross-account access control.
- **API Gateway throttling** on `POST /submissions` specifically — the
  current CORS config allows any origin; a public-facing deployment
  should add a WAF rule or API Gateway usage plan to prevent someone
  from scripting mass submissions against your (rate-limited, proxy-
  budget-constrained) `fetch_captions` function.

---

## 10. Testing strategy

- **URL parsing** (`youtube_url.py`): pure function, trivially unit
  tested against every known URL shape plus deliberately malformed
  input — no AWS mocking needed.
- **`submit_handler.py` idempotency logic**: `moto`'s DynamoDB and SQS
  mocks, asserting the exact three-way branch (new video → SQS message
  sent; in-flight video → 202 with no new SQS message; completed video →
  200 with cached data, no new SQS message).
- **`fetch_captions.py`**: mock `youtube_transcript_api`'s client
  methods directly (don't hit real YouTube in CI) to exercise every
  branch — success, each of the three "no transcript" outcomes, and the
  `BlockedRetryable` path — and assert the OUTPUT SHAPE is complete in
  every branch (this is the exact property the Step Functions
  `MergeFetchResult` reshape state depends on not breaking).
- **`transcribe_callback.py`'s output parser**: unit test
  `_parse_transcribe_output` against a small fixture JSON shaped like a
  real Transcribe batch result, asserting correct 10-second bucketing
  and confidence averaging — this function has no AWS calls in its core
  logic, so it needs no mocking at all, just fixture data.
- **`review_callback.py`'s HMAC logic**: test that a tampered
  `decision` query param fails signature verification, that a replayed
  `review_id` after consumption returns "already handled," and that
  `hmac.compare_digest` (not `==`) is actually what's used — a
  regression here is a real security bug, not just a test nicety.
- **Step Functions definition itself**: the AWS Step Functions Local
  Docker image lets you run the ASL definition against mocked Lambda
  responses without touching a real AWS account — the right tool for
  validating the CHOICE-state routing logic (does `DISABLED` +
  `respect_disabled=true` actually reach `MarkDisabledByCreator`, etc.)
  end-to-end.
- **End-to-end**: LocalStack for S3/DynamoDB/SQS/Secrets Manager/
  EventBridge; Step Functions and Transcribe are the two services
  LocalStack's free tier doesn't meaningfully emulate, so those legs
  still need a real (or scaled-down, single-video) AWS smoke test.

---

## 11. Operational runbook sketch

1. **A Step Functions execution has been running far longer than
   expected** → check whether it's parked on `RequestHumanReview` (an
   unanswered review email) or `StartTranscribeJob` (an unusually long
   video, or a Transcribe job that finished but `transcribe_callback`
   failed to resume it — check that Lambda's DLQ,
   `terraform/12_eventbridge.tf`'s `eventbridge_dlq`, first).
2. **`fetch-captions-errors` alarm fires** → check whether errors are
   concentrated (proxy pool exhausted — check Webshare's own dashboard
   for the account's current ban/health status) or spread thin across
   many videos (more likely a genuine mix of disabled-captions/
   unavailable videos, which is expected background noise, not an
   incident).
3. **`submissions-dlq-depth` alarm fires** → a message failed
   `start_workflow.py` three times (the queue's `maxReceiveCount`).
   Check CloudWatch Logs for that function first; a common cause is a
   transient `StartExecution` throttle that should have self-healed —
   if so, redrive the DLQ manually once the underlying issue clears.
4. **QuickSight dashboard shows stale data** → check the
   `materialize_categories` Glue job's last successful run (it's
   trigger-chained off `raw_to_parquet`, so a failure upstream silently
   stalls both) before assuming the SPICE refresh schedule itself is
   broken.

---

## 12. What's deliberately NOT built (scope, stated honestly)

"Take it from basic to ultra advanced" has no natural ceiling, so here's
where this build draws the line, and what a genuine next iteration would
add:

- **Per-submission notification preferences** (a webhook or personal
  email captured at submission time, notified when THAT video's
  transcript is ready) — currently only the shared `ops_alarms` /
  `review_requests` topics exist; a real multi-user deployment wants
  per-submitter delivery, which needs either a dynamic SNS subscription
  per submission or a lighter-weight direct-send (SES) keyed off the
  jobs table.
- **Non-English transcript translation** (Amazon Translate as a further
  pipeline stage) so `athena/queries.sql`'s keyword-search queries work
  across languages, not just within whatever language a transcript
  happens to be in.
- **Semantic/vector search over transcript text** (embeddings + a
  vector store, or Athena's newer ML-function integrations) — the
  current design only supports exact-substring `LIKE` search, which is
  the SQL-first, no-new-infrastructure starting point, not the ceiling.
- **A confirm-then-POST review flow** (replacing the single-GET
  approve/reject link) to fully close the link-preview-crawler race
  condition noted in §7, at the cost of the extra UI page.

None of these are hard blockers on anything above — they're the natural
"if this became a real product" next layer, scoped out here to keep this
build's surface area matched to what was actually asked for.
