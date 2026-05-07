# Post-Call Processing Pipeline — Design Document (v2)

**Author:** Pratham Nigam
**Date:** 2026-05-06

---

## 1. Assumptions

These are the assumptions I made while designing v2. They underpin every
decision below; if any of them is wrong, the design changes.

1. **Business cares about hot calls within minutes, cold calls within hours.**
   A confirmed booking that the sales team can't act on for 8 hours is a
   missed revenue event. A "not interested" disposition that lands in the
   dashboard 2 hours later is fine — nobody is waiting on it.
2. **The cheapest "is this call urgent?" signal is the transcript itself.**
   A single regex pass over the customer's words separates clear-cut
   outcomes ("not interested", "wrong number", "already booked") from
   ambiguous ones with high accuracy at zero token cost. Reading the
   sample fixtures, this catches roughly half the calls.
3. **Customers will accept hard rate limits if they have a guaranteed share.**
   Customer A buying 20K TPM of capacity expects 20K TPM regardless of
   what Customer B is doing. A noisy neighbour starving a paying customer
   is unacceptable.
4. **LLM provider rate limits are HARD limits.** A 429 response is the
   provider saying "you broke the contract"; the system must never produce
   them. We treat the published TPM/RPM as a ceiling and operate strictly
   below it.
5. **Recording delivery is best-effort but observable.** Exotel typically
   delivers in 10–30s, occasionally 90s+. Missing recordings are not a
   call-blocker for the analysis path, but they MUST always produce an
   alertable event — silent loss is unacceptable.
6. **At-least-once delivery + idempotent effects > exactly-once delivery.**
   Building real exactly-once is expensive and rarely needed. We commit
   to at-least-once execution with idempotent handlers; the few duplicate
   side-effects (e.g. CRM webhook) are deduped at the receiver.
7. **Postgres is the system of record.** Redis is a cache and a transport.
   Anything that must survive a 30-second blip lives in Postgres.
8. **Workers are stateless and replaceable.** Visibility timeouts on the
   outbox row are the only worker-state we trust. A worker dying mid-job
   is fine — another picks it up.
9. **The dialler can apply proportional backpressure.** It already calls
   the system before each dispatch (today asking the circuit breaker);
   we replace that yes/no answer with a probability, no dialler change
   needed beyond honouring it.
10. **Mock infrastructure is sufficient for the assessment.** The LLM,
    Exotel and S3 are mocked. The Postgres-shaped behaviours (outbox,
    audit log, DLQ, usage ledger) are exercised via in-memory fakes whose
    SQL equivalents are documented inline in each module.

---

## 2. Problem Diagnosis

The v1 system has five compounding failure modes at 100K-call scale:

1. **No rate-limit awareness.** `LLM_TOKENS_PER_MINUTE` is in `config.py`
   but no code reads it before firing a request. At burst, the system
   pegs the provider and gets 429s.
2. **Single-queue, single-priority Celery.** A "wrong number" 10s call and
   a confirmed booking sit in the same FIFO line. One customer's burst
   delays another's.
3. **45-second recording sleep blocks LLM.** Recording fetch and
   transcript analysis are unrelated, but they share one Celery task
   that sleeps 45s before doing anything. Throughput is bounded by the
   sleep, not by the LLM.
4. **Two retry mechanisms that don't talk.** Celery retries AND the
   `PostCallRetryQueue` retry, both Redis-backed. A Redis blip loses
   both. A failed task can be re-tried twice.
5. **Binary 1800s circuit breaker.** At 90% LLM utilisation the dialler
   freezes for half an hour. The sales team notices before the engineers.
   No gradual fallback.

The single highest-leverage fix is a rate-limit-aware scheduler that
also lets us **not run the LLM** on calls where the outcome is obvious.
Everything else (durability, observability, recording) is downstream of
that.

---

## 3. Architecture Overview

```
                  ┌─────────────────────────────────────────────────┐
                  │  POST /session/{sid}/interaction/{iid}/end       │
                  │  (FastAPI, < 50 ms)                              │
                  └───────────────────┬─────────────────────────────┘
                                      │
              ┌───── triage(transcript) ────────┐
              │  cold lane no-op?  → DONE       │  (heuristic only,
              │  hot lane?         → enqueue    │   zero LLM tokens)
              │  ambiguous?        → enqueue    │
              └─────────────────┬───────────────┘
                                │
                                ▼
                ┌─────────────────────────────────────┐
                │  interaction_jobs (Postgres outbox)  │
                │  one row per logical step,           │
                │  idempotency_key, attempts, state    │
                └──────┬───────────────────────┬──────┘
                       │                       │
              hot lane ▼              cold lane ▼
              ┌──────────────────┐  ┌──────────────────┐
              │  worker (claim)  │  │  worker (claim)  │
              └──────┬───────────┘  └──────────┬───────┘
                     │                         │
                     ▼                         ▼
              ┌──────────────────────────────────────┐
              │  budget_service.acquire(customer, N)  │
              │     ├── per-customer reservation      │
              │     └── shared headroom (token bucket)│
              │       Lua-atomic check-and-debit       │
              └──────┬───────────────────┬───────────┘
                ALLOW│             DEFER │ (requeue, no attempt cost)
                     ▼                   ▼
                ┌────────────┐      ┌────────────┐
                │  LLM call   │      │ outbox.defer│
                └────┬────────┘      └────────────┘
                     │
                     ▼
        ┌────────────────────────────────────┐
        │  analysis_results (canonical row)   │
        │  + interaction_metadata (dashboard) │
        │  + token_usage (billing ledger)     │
        └────┬───────────────────────────────┘
             │ enqueues downstream jobs
             ▼
        ┌────────────────────────────┐    ┌──────────────────────┐
        │ signal_dispatch (durable)   │    │  recording_upload    │
        │ → lead_stage_update         │    │  (independent poller │
        │ → crm_push                  │    │   with exp. backoff) │
        └────────────────────────────┘    └──────────────────────┘
                                                       │
                                              fail → DLQ + alert
```

### Key design decisions

1. **Postgres outbox replaces Celery as the system of record.** Celery
   becomes an executor. Workers claim from `interaction_jobs` with a
   visibility timeout; a broker restart is a no-op. (`src/services/outbox.py`)
2. **Cheap heuristic triage runs BEFORE any LLM call.** Clear-cut cold
   outcomes never spend tokens. Hot outcomes are flagged for the hot lane.
   (`src/services/triage.py`)
3. **Two-tier token bucket: per-customer reservation + shared headroom.**
   Atomic via Redis Lua. Sized off the published provider limit.
   (`src/services/rate_limiter.py`)
4. **Recording polling is its own job type.** Exponential backoff with
   jitter, bounded attempts, every attempt audited, exhaustion → DLQ +
   alert. (`src/services/recording_poller.py`)
5. **Append-only audit log on every state transition.** Postgres-backed,
   carries `correlation_id`. (`src/services/audit_log.py`)
6. **Proportional dialler backpressure replaces the binary freeze.**
   Admission probability tapers between soft and hard utilisation.
   (`src/services/backpressure.py`)
7. **Result writes are idempotent.** Canonical row in `analysis_results`,
   keyed by interaction; the dashboard reads `interaction_metadata`
   patched by the same write. A retry that runs after success is a no-op.
   (`src/services/analysis_worker.py`)

---

## 4. Rate Limit Management

### How we track usage

A single token bucket per (customer, dimension) and a single shared bucket
per dimension. Token bucket because it captures *sliding-window* spend
correctly; fixed-window counters allow 2× bursts at boundaries.

The bucket is implemented as a Redis hash `{tokens, updated_ms}` mutated
by an atomic Lua script (`src/services/rate_limiter.py`):

```
elapsed = now_ms - updated_ms
tokens  = min(capacity, tokens + elapsed * rate_per_sec)
if tokens >= cost:
    tokens -= cost
    return ALLOW
else:
    return DENY, retry_after_ms = (cost - tokens) / rate_per_sec
```

Capacity is sized to one minute of refill (i.e. `capacity == rate_per_min`)
so the bucket can absorb a one-minute burst without the limiter ever
denying healthy traffic.

We track BOTH dimensions the provider rate-limits on:

* **TPM** (tokens per minute): correlates with LLM cost.
* **RPM** (requests per minute): correlates with API call count.

A request must satisfy both. A 10-turn transcript may pass RPM but blow
TPM; a flood of tiny calls may pass TPM but blow RPM. We check both.

### How we decide what to process now vs. defer

Two switches:

1. **Triage lane** (`hot` / `cold` / `skip`). Hot calls compete for
   immediate budget; cold calls are picked up only when there is global
   headroom (i.e. no hot call is waiting for the same bucket). The
   outbox dispatcher always prefers the hot lane.
2. **Budget acquire** returns one of:
   * `ALLOW`: bucket had headroom; the LLM call proceeds.
   * `DEFER`: bucket empty, retry after `retry_after_ms`. The job is
     **requeued without consuming an attempt** so a 100K burst doesn't
     burn through retry budgets.
   * `REJECT`: customer hit a per-day hard cap; permanent, audited, DLQ'd.

### Recovery when limits are hit

The bucket keeps refilling at the configured rate. Deferred jobs are
retried after the precise computed delay. There is no "give up and wait
30 minutes" path — the system is always trying to make progress.

If the limiter itself fails (Redis unavailable), the limiter **fails
closed**: it returns DEFER. We'd rather wait than 429 the provider.

---

## 5. Per-Customer Token Budgeting

### Allocation model

Every customer has a row in `customers` with a
`tokens_per_minute_reservation`. The global TPM is split into three
zones, with fractions configurable at runtime:

```
total = LLM_TOKENS_PER_MINUTE
        ├── reserved fraction  (default 60%): sum of customer reservations
        ├── shared fraction    (default 30%): fair-share pool for any customer
        └── safety margin      (default 10%): never spent, protects from
                                              provider-side accounting drift
```

Sum of reservations across customers is enforced (at customer-onboarding
time) to be ≤ total × reserved_fraction. Anything above is held back.

### Acquire order

1. Try the customer's reserved bucket. If both TPM and RPM allow, ALLOW.
2. Else try the shared bucket. If both allow, ALLOW (returns `bucket="shared"`).
3. Else DEFER with the larger of the two retry_after_ms.

### Guarantees per scenario

* **Customer A pre-allocated 20K TPM.** Their reserved bucket holds 20K
  tokens of capacity. Even if Customer B is in a campaign burst,
  Customer A's reservation is untouched. A's calls always serve from
  their reservation when there is headroom; only when their reservation
  is drained do they compete with B for shared headroom.
* **Customer A exceeds their budget.** Excess traffic falls through to
  the shared pool. If the shared pool has headroom, A is served (this
  is how unallocated headroom is utilised). If both A's reservation
  and the shared pool are empty, A's calls DEFER — they don't 429,
  and they don't starve other customers' reservations.
* **What happens to unallocated headroom?** Customers with no
  reservation share the shared pool with reservation-overflow traffic
  from paying customers. We never let one customer consume the entire
  shared pool: priority is honoured by the Redis bucket's natural FIFO
  refill behaviour, and the tie-break in the dispatcher uses the
  `customers.priority` column.

This is enforced in test `test_per_customer_budget_isolation` — A's
reservation is exhausted, but B's reservation is untouched.

---

## 6. Differentiated Processing

Three lanes: **hot**, **cold**, **skip**.

Lane is determined by the cheap pre-LLM triage in
`src/services/triage.py`. The triage runs against the customer's words
(plus, for hot signals, the agent's confirmation phrases) using a
keyword/regex pass with noisy-OR confidence aggregation.

| Lane   | Examples                                                   | Behaviour                                            |
|--------|------------------------------------------------------------|------------------------------------------------------|
| `hot`  | rebook_confirmed, demo_booked, escalation_needed           | Always runs LLM; preferred by the dispatcher.        |
| `cold` | not_interested, wrong_number, already_done, hinglish_ambig | Skips LLM if heuristic confidence ≥ 0.85; else LLM in cold lane. |
| `skip` | < 4 turns                                                  | Heuristic-only result; LLM never invoked.            |

Why heuristic for cold and not LLM:

* The LLM costs tokens. The triage costs nothing.
* A "wrong number" / "not interested" outcome is mechanically detectable
  and **deterministic**. There is no upside to spending tokens to confirm
  something a regex already proved.
* For ambiguous cold calls, we still run the LLM but in the cold lane,
  which is preempted by hot work. Worst case: the result lands an hour
  later than ideal — fine for a "thinking about it" lead.

Why we always run the LLM on hot calls (even if heuristics fired):

* The downstream WhatsApp / CRM payload needs the *entities* (slot,
  amount, name) and the *summary*, not just the call_stage. Skipping the
  LLM on a high-value call to save 1500 tokens is the wrong trade.

Hot and cold lane workers can be the same physical pool (the dispatcher
picks hot first), or scaled independently. The interface is the same.

---

## 7. Recording Pipeline

Replaces `asyncio.sleep(45)` with a separate durable job
(`src/services/recording_poller.py`).

### Mechanism

1. The endpoint enqueues a `recording_upload` job alongside the analysis
   job. They are independent — the analysis can land in 4 seconds even
   if the recording poller is on minute 3.
2. The poller calls Exotel. Outcomes:
   * `200` + URL → download + S3 upload → `outbox.complete` → audit success.
   * `404` (not ready) → `outbox.fail` with backoff `next_run_at`. Burns
     an attempt because we want to bound polling.
   * `5xx` / network error → same retry path; failure reason logged.
3. Backoff is exponential with full jitter: 5s, 10s, 20s, 40s, 80s, 160s,
   then capped at 300s. With `RECORDING_POLL_MAX_ATTEMPTS=8`, the cumulative
   wall time is ~10 minutes — enough for Exotel's worst observed delivery
   window.
4. Attempts exhausted → `DEAD_LETTERED`, full payload preserved in `dlq_jobs`.

### What the on-call engineer sees

For any interaction:

```sql
SELECT created_at, event_type, severity, details
FROM interaction_audit_log
WHERE interaction_id = $1 AND event_type LIKE 'recording_%'
ORDER BY created_at;
```

Returns one row per attempt, with the timestamp, the failure reason if
any, and the s3_key on success. There is no "the recording is just gone"
outcome — every step is recorded.

### Alerts

* `recording_poller_dlq_depth > 10` (5-minute window) → page.
* p95(`recording_uploaded.attempts_used`) > 6 → ticket (Exotel slow).

---

## 8. Reliability & Durability

### Why not Celery alone

Celery's broker is its source of truth. A broker restart loses anything
that wasn't acked, and `acks_late=True` workers re-receive jobs at the
back of the queue — which at 100K depth is an SLA disaster.

### What replaces it

A Postgres `interaction_jobs` table is the source of truth.

* `idempotency_key` (UNIQUE) — re-enqueues are no-ops, regardless of
  source (Celery redelivery, retry queue, manual replay).
* `state` (PENDING/CLAIMED/SUCCEEDED/FAILED/DEAD_LETTERED) with
  `claimed_until` for visibility timeout.
* `next_run_at` — the dispatcher picks earliest-runnable.
* `attempt`, `max_attempts`, `last_error` — full retry state per job.

Workers consume by:

```sql
UPDATE interaction_jobs
SET state='CLAIMED', claimed_until=NOW()+interval '120s',
    claimed_by=$worker, attempt=attempt+1, updated_at=NOW()
WHERE id = (
  SELECT id FROM interaction_jobs
  WHERE state IN ('PENDING','FAILED')
     OR (state='CLAIMED' AND claimed_until <= NOW())
  AND next_run_at <= NOW()
  AND ($lane IS NULL OR lane=$lane)
  ORDER BY (lane='hot') DESC, next_run_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

`SKIP LOCKED` keeps multiple workers from contending. If a worker dies
between CLAIM and complete/fail, the visibility timeout expires and the
next claim picks the row up (test: `test_worker_crash_mid_job_is_reclaimed`).

### Dead letter queue

Anything that exhausts retries lands in `dlq_jobs` with the full payload,
last error, and correlation_id. An operator can replay any DLQ entry with:

```sql
UPDATE interaction_jobs
   SET state='PENDING', attempt=0, next_run_at=NOW(), last_error=NULL
 WHERE id=$1;
UPDATE dlq_jobs SET replayed_at=NOW() WHERE id=$2;
```

---

## 9. Auditability & Observability

### What we log

Every state transition writes a row to `interaction_audit_log` AND a
structured stdout log line, both carrying:

| Field           | Why it's there                                                                |
|-----------------|-------------------------------------------------------------------------------|
| `interaction_id`| Filter to a single call.                                                       |
| `correlation_id`| Same across all jobs / events for one interaction.                            |
| `customer_id`   | Per-customer dashboards, per-customer alerts, billing reconciliation.          |
| `campaign_id`   | Per-campaign throughput / cost.                                                |
| `event_type`    | Stable enum: `call_ended`, `triaged`, `job_enqueued`, `job_claimed`, `job_succeeded`, `job_failed`, `job_deferred`, `job_dead_lettered`, `llm_started`, `llm_failed`, `llm_provider_429`, `analysis_completed_llm`, `analysis_completed_heuristic`, `recording_poll_attempt`, `recording_not_ready`, `recording_uploaded`, `signal_dispatched`, `lead_stage_updated`, `crm_push_attempted`, `budget_deferred`, `budget_rejected`, `handler_crashed`. |
| `severity`      | INFO / WARN / ERROR — feeds the alert channel selection.                       |
| `details`       | Free-form JSONB — token counts, retry delays, error messages.                 |
| `created_at`    | Strict ordering for replay.                                                    |

### Debugging an interaction 3 days later

```sql
SELECT created_at, event_type, severity, details
FROM interaction_audit_log
WHERE interaction_id = '...'
ORDER BY created_at;
```

Reads as a diary. Test `test_full_audit_trail_for_one_interaction` pins
the event sequence for one happy-path interaction.

### Alerts

| Condition                                              | Threshold                             | Action  |
|--------------------------------------------------------|---------------------------------------|---------|
| Global TPM utilisation                                 | > 85% for 2 minutes                   | Ticket  |
| Global TPM utilisation                                 | > 95% for 1 minute                    | Page    |
| Hot-lane queue depth                                   | > 1000                                | Page    |
| `job_dead_lettered` rate                               | > 5/min                               | Page    |
| `recording_poller_dlq_depth`                           | > 10                                  | Page    |
| LLM 429 events (`llm_provider_429`)                    | > 0                                   | Page    |
| Per-customer TPM utilisation                           | > 90% of reservation, 5 min sustained | Customer-facing notice |

---

## 10. Data Model

See `data/migrations/001_postcall_v2.sql` for the full migration. New
tables:

* `customers` — per-tenant config: reservation, priority, JSONB config.
* `interaction_jobs` — durable outbox; replaces Celery as source of truth.
* `interaction_audit_log` — append-only event trail per interaction.
* `recording_jobs` — decoupled recording polling state.
* `analysis_results` — canonical analysis output, keyed by interaction;
  separate from the JSONB hot-cache so retries can't overwrite history.
* `token_usage` — append-only billing ledger.
* `dlq_jobs` — dead-letter store with full payload preserved.

Existing `interactions` is extended with: `lane`, `triage_label`,
`triage_confidence`, `analysis_status`, `recording_status`, `correlation_id`.

```sql
-- Excerpt — see migration file for full DDL
CREATE TABLE customers (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    tokens_per_minute_reservation INTEGER NOT NULL DEFAULT 0,
    priority SMALLINT NOT NULL DEFAULT 100,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE interaction_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    interaction_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    campaign_id UUID NOT NULL,
    job_type VARCHAR(40) NOT NULL,
    lane VARCHAR(10) NOT NULL DEFAULT 'cold',
    state VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    correlation_id UUID NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 8,
    next_run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_until TIMESTAMPTZ,
    claimed_by VARCHAR(128),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_jobs_dispatch
    ON interaction_jobs(state, lane, next_run_at)
    WHERE state IN ('PENDING','FAILED');
```

---

## 11. Security

Sensitive data inventory:

| Data                                                     | Where it lives                | Sensitivity |
|----------------------------------------------------------|-------------------------------|-------------|
| Call transcripts (PII, conversation contents)            | `interactions.conversation_data` (JSONB) | High |
| Lead PII (name, phone, email)                            | `leads`                       | High         |
| Audio recordings                                         | S3 (`recordings/...mp3`)      | High         |
| LLM prompts (contain transcript + metadata)              | Outbound HTTPS to provider    | High         |
| Token usage / billing                                    | `token_usage`, `customers`    | Medium       |
| Audit log details                                        | `interaction_audit_log`       | Medium       |

Protection plan:

* **Transit.** TLS 1.2+ end-to-end. mTLS on Exotel webhook ingress; the
  LLM provider is contacted over TLS with pinned certs in production.
* **Rest — recordings.** S3 bucket has SSE-KMS with a customer-scoped CMK.
  Bucket policy denies public access; access is via VPC endpoint only.
  Object lifecycle: archive to Glacier after 30 days, delete after the
  customer-configured retention period (default 180 days).
* **Rest — Postgres.** Disk encryption (cloud-managed) plus column-level
  pgcrypto for `conversation_data` and `lead_data` (transparently
  decoded by application; the database on disk holds ciphertext). The
  application reads `lead.phone` only when joining for display; bulk
  query patterns avoid PII columns by default.
* **PII redaction in audit log.** `interaction_audit_log.details`
  intentionally never contains the transcript; only IDs, timings, and
  status fields. This means the audit log can be retained longer than
  the transcript without policy concerns.
* **Per-customer key separation.** S3 KMS keys are per-customer; if a
  customer leaves, we delete their CMK and their recordings become
  cryptographically inaccessible.
* **LLM data handling.** Provider contract specifies "no training on
  customer data". Prompts are hashed and stored against the
  `analysis_results.raw_response` row only as needed for billing dispute
  evidence; full prompt text is never logged in stdout.
* **Access control.** Postgres has separate roles for the API
  (read/write to interactions, sessions, leads), the worker
  (read/write to interaction_jobs, audit log, results), and ops
  (read-only across all tables).
* **Secrets.** `LLM_API_KEY` and DB credentials come from Vault/Secrets
  Manager, never committed. The repo's mock key is clearly named
  `sk-mock-key-for-assessment`.

---

## 12. API Interface

The contract changes from v1 are intentionally small — they're additive
and backwards compatible:

* Same path: `POST /session/{sid}/interaction/{iid}/end`.
* Same request body — `call_sid`, `duration_seconds`, `call_status`,
  `additional_data` are unchanged.
* Response now includes:
  * `correlation_id` — let the caller follow the call through audit logs.
  * `lane` — `hot` | `cold` | `skip`. Lets the caller know whether to
    expect immediate or deferred analysis.
  * `triage_label` — useful for clients that want to short-circuit
    waiting for the dashboard.

Behaviour change visible to callers:

* The endpoint **no longer fires `signal_jobs` and `update_lead_stage`
  with empty analysis_result.** All downstream effects come from
  durable jobs in the outbox. This fixes the v1 footgun where every
  long-call interaction caused two dispatches: one empty, one real.
  Downstream services now receive exactly one signal per outcome.
* The endpoint never blocks on Redis or Celery — outbox enqueue is a
  single Postgres INSERT, so the response is fast.

---

## 13. Trade-offs & Alternatives Considered

| Option                                        | Why considered                                               | Why rejected / what I chose                                                                                                                                       |
|-----------------------------------------------|--------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Real Kafka / RabbitMQ for queueing             | Battle-tested, lots of tooling.                              | Adds another piece of infra. Postgres outbox + visibility timeout meets the durability bar with infra we already run.                                              |
| Use a small "classifier" LLM for triage        | Higher accuracy than regex.                                  | A cheap LLM is still a token + RPM consumer. Regex is free and deterministic; per-customer overrides give us an escape hatch for missed cases.                     |
| Skip the LLM entirely; classify by transcript embedding similarity | Cheap, no rate limit.                            | Embeddings still need GPU/$$ and add an inference step. We get most of the value from regex + always-on LLM for ambiguous calls.                                   |
| Allow 429s and rely on Celery retries          | Simpler — no limiter.                                        | The dialler / customers see backed-up queues; the provider may rate-limit our entire account. Hard NO from constraint #2.                                          |
| Per-agent rate limiter (v1's approach)         | Simple key, easy reasoning.                                  | Wrong granularity. The LLM sees one account; rate limits apply across agents. Per-customer reservation maps to the contract; per-agent doesn't.                    |
| Fixed-window minute counters                   | Easier to implement than a token bucket.                     | Lets a 2× burst through at the boundary (last 10s of minute X + first 10s of minute X+1). Token bucket smooths properly.                                          |
| Rerun signal_jobs after every retry            | Simple correctness.                                          | Causes downstream double-dispatch (CRM / WhatsApp). Idempotency at the source (idempotency_key on jobs) is cleaner than dedup at every receiver.                   |
| Use Celery countdowns for recording poll       | Built-in, no custom code.                                    | Celery countdowns go to the back of the broker queue; with 100K postcall jobs ahead, "retry in 5s" can become "retry in 2 hours". Outbox `next_run_at` is precise. |
| Single-tenant rate limiter (no reservations)   | Simpler bucket math.                                         | Cannot give customers a contractual TPM guarantee. Disqualifies multi-tenant SaaS.                                                                                 |
| Keep `interaction_metadata` as the only result store | One column to read.                                  | Retries can overwrite the previous good result. We add a separate `analysis_results` table as the canonical store; `interaction_metadata` remains the dashboard cache. |
| Drop short-call check at endpoint, do it in worker | Less endpoint work.                                      | The endpoint already has the transcript; running the check there saves a worker enqueue. We do BOTH: endpoint sets lane='skip', worker re-checks defensively.      |

---

## 14. Known Weaknesses

* **In-memory stores in the assessment harness.** The outbox, audit log,
  DLQ, usage tracker, and analysis store are in-memory for the
  assessment to keep `pytest` self-contained. Each module documents the
  Postgres SQL it would issue in production. Swapping to a real DB is a
  one-file change per module (replace the dict with an asyncpg call).
* **Token estimate vs actual.** We debit the bucket with the *average*
  token estimate. If a transcript is 2× the average, we under-debit the
  limiter for that one call. Mitigations: (a) we apply the diff to the
  daily ledger so billing is exact; (b) the safety margin (10% of
  global TPM) absorbs the drift; (c) for very long transcripts we could
  pre-count input tokens client-side before debiting (not yet
  implemented).
* **Soft starvation risk for cold lane.** If hot is constantly busy,
  cold can starve. We mitigate with a max age on cold jobs (currently
  not implemented; would add `must_run_by` to `interaction_jobs`).
* **No per-region rate limit awareness.** If we ever fail over to a
  secondary LLM region with its own quota, the limiter needs a
  region key. Not addressed; out of scope.
* **DLQ replay is manual.** We provide the SQL but no UI; for the
  assessment scope this is fine.
* **Triage regex coverage is finite.** Some edge-case Hinglish phrasings
  will fall through to "ambiguous → cold lane → LLM". That's the
  designed-for failure mode (cost: a token spend); it's not a correctness
  failure.

---

## 15. What I Would Do With More Time

1. **Real Postgres bindings.** Replace the in-memory outbox / audit / DLQ
   with asyncpg-backed implementations. The SQL is already documented
   in each module.
2. **Concurrency tests with real Redis.** Spin up a Redis container in
   CI and exercise the Lua bucket under genuine parallelism (the fake
   Redis in tests is single-threaded).
3. **Cold-lane starvation guard.** Add `must_run_by` to
   `interaction_jobs`; the dispatcher promotes any cold job past its
   deadline to hot.
4. **Per-customer config UI / API.** The `customers.config` JSONB column
   is designed to hold per-customer triage overrides, alert thresholds,
   and CRM webhook configs — none of which require redeploys today, but
   need an API to be useful.
5. **CRM push reliability.** The current handler is a mock. A real one
   would need per-customer auth, signed payloads, idempotency at the
   receiver, and retry telemetry.
6. **Token forecasting / autoscaling.** Use `token_usage` to predict the
   next 10 minutes' spend per customer; pre-warm worker pools and warn
   the customer when their reservation will be exceeded.
7. **Recording streaming to S3 directly from Exotel.** Eliminates the
   download → upload step; halves wall-clock time for the recording job.
8. **Encryption at rest implementation.** The plan in §11 is real; the
   pgcrypto column-level encryption needs a small migration + a tiny
   model-layer codec.

---

## How to Run

```bash
# Install deps and run the v2 acceptance suite (no docker required for tests)
pip install -r requirements.txt
pytest tests/ -v

# Full local stack
docker-compose up -d   # Postgres + Redis; v2 schema is auto-applied

# Production worker (the loop is in src/services/job_runner.py):
python -m src.services.job_runner   # would `run_forever()`
# Celery is still wired; the v2 task is `postcall.dispatch_jobs`
celery -A src.tasks.celery_app worker -Q postcall_hot,postcall_cold,postcall_processing
```

The v2 acceptance suite (17 tests) covers each AC1–AC10; map:

| AC  | Test                                                        |
|-----|-------------------------------------------------------------|
| AC1 | `test_rate_limiter_never_surfaces_429`                      |
| AC2 | `test_per_customer_budget_isolation`                        |
| AC3 | `test_worker_crash_mid_job_is_reclaimed`, `test_failed_job_eventually_dead_letters_with_payload` |
| AC4 | `test_recording_poller_retries_with_backoff_then_succeeds`, `test_recording_poller_dead_letters_after_max_attempts` |
| AC5 | `test_full_audit_trail_for_one_interaction`                 |
| AC6 | `test_every_failure_has_correlation_id`                     |
| AC7 | `test_backpressure_is_proportional_not_binary`              |
| AC8 | `test_triage_short_call_skips_llm`, `test_triage_clear_no_op_calls_dont_need_llm` |
| AC9 | This document — §1 (assumptions) and §13 (trade-offs).       |
| AC10| This document — §11 (security).                              |
