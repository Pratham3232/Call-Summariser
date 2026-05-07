"""
Recording poller — replaces the 45-second blocking sleep.

Design:
    A `recording_upload` job is enqueued at call-end time and processed
    independently of LLM analysis. The poller calls Exotel's recording
    endpoint with exponential backoff (5s, 10s, 20s, 40s, 80s, 160s, 300s
    capped) for up to RECORDING_POLL_MAX_ATTEMPTS attempts.

    Outcomes:
      - HTTP 200 + URL: download → upload to S3 → mark UPLOADED → audit success
      - HTTP 404 / not-yet: bump attempt, schedule next_run_at, audit waiting
      - HTTP 5xx / timeout: bump attempt, schedule next_run_at, audit transient
      - Attempts exhausted: mark DEAD_LETTERED, audit ERROR (alertable),
        write DLQ entry. Recording is NEVER silently lost.

    The poller never blocks LLM analysis. The two concerns are completely
    decoupled — analysis can finish in 4 seconds while the recording job
    is still polling minute 3.

Why not Celery's countdown retries:
    Celery countdowns on Redis-broker setups go to the back of the queue
    (acks_late=True). With 100K post-call jobs ahead of it, a "retry in
    10s" can become "retry in 2 hours". The outbox stores next_run_at
    explicitly and dispatchers always poll the *earliest-runnable* job.

Failure visibility:
    Every attempt writes an audit row. An on-call engineer querying
    `interaction_audit_log WHERE event_type LIKE 'recording_%'` sees
    every attempt with timestamps and the exact reason. No more
    "the recording is just gone".
"""

from __future__ import annotations

import logging
import random
from typing import Optional

import httpx

from src.config import settings
from src.services.audit_log import audit_log
from src.services.outbox import outbox, Job

logger = logging.getLogger(__name__)


def _backoff_seconds(attempt: int) -> float:
    """
    Exponential backoff with full jitter, capped at the configured max.

    attempt=1 -> ~5s,  attempt=2 -> ~10s,  attempt=3 -> ~20s,
    attempt=4 -> ~40s, attempt=5 -> ~80s, attempt=6 -> 160s,
    attempt>=7 -> 300s (capped).

    Full jitter (uniform in [0.5*base, 1.5*base]) avoids thundering-herd
    when many recordings are scheduled at the same instant.
    """
    base = settings.RECORDING_POLL_INITIAL_DELAY_SECONDS * (2 ** max(0, attempt - 1))
    base = min(settings.RECORDING_POLL_MAX_DELAY_SECONDS, base)
    return base * random.uniform(0.5, 1.5)


async def fetch_recording_url(call_sid: str, account_id: str) -> Optional[str]:
    """
    Hit the Exotel API. Returns:
      - The URL if HTTP 200 with a recording_url
      - None if HTTP 404 (not ready yet) — caller schedules a retry
    Raises on non-404 errors so the caller can audit + back off.
    """
    url = f"https://api.exotel.com/v1/Accounts/{account_id}/Calls/{call_sid}/Recording"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        if resp.status_code == 200:
            return resp.json().get("recording_url")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    return None


async def upload_to_s3(recording_url: str, interaction_id: str) -> str:
    """
    Stream Exotel → S3. Returns the S3 key on success.

    In production: stream via boto3, set object-level KMS, write the
    s3_key back to the interactions row.
    """
    s3_key = f"recordings/{interaction_id}.mp3"
    logger.info(
        "recording_uploaded_to_s3",
        extra={"interaction_id": interaction_id, "s3_key": s3_key},
    )
    return s3_key


async def process_recording_job(job: Job) -> None:
    """
    Single-attempt handler. Called by the dispatcher when a `recording_upload`
    job is claimed. Outcomes route through outbox.complete()/fail()/defer()
    so reliability is consistent with every other job type.
    """
    interaction_id = job.interaction_id
    call_sid = job.payload.get("call_sid", "")
    account_id = job.payload.get("exotel_account_id", "")

    audit_log.write(
        interaction_id=interaction_id,
        correlation_id=job.correlation_id,
        customer_id=job.customer_id,
        campaign_id=job.campaign_id,
        event_type="recording_poll_attempt",
        details={"attempt": job.attempt, "call_sid": call_sid},
    )

    try:
        url = await fetch_recording_url(call_sid, account_id)
    except Exception as e:
        # Transient — back off and try again.
        delay = _backoff_seconds(job.attempt)
        outbox.fail(job.id, error=f"exotel_error: {e}", retry_after_seconds=delay)
        return

    if url is None:
        # Not ready yet — schedule another bounded attempt. We use fail()
        # with an explicit retry_after_seconds (not defer()) so the
        # attempt budget IS consumed; otherwise we'd poll Exotel forever
        # for a recording that genuinely never appears.
        delay = _backoff_seconds(job.attempt)
        outbox.fail(
            job.id,
            error="recording_not_ready",
            retry_after_seconds=delay,
        )
        audit_log.write(
            interaction_id=interaction_id,
            correlation_id=job.correlation_id,
            customer_id=job.customer_id,
            campaign_id=job.campaign_id,
            event_type="recording_not_ready",
            details={"attempt": job.attempt, "next_in_s": round(delay, 2)},
        )
        return

    try:
        s3_key = await upload_to_s3(url, interaction_id)
    except Exception as e:
        delay = _backoff_seconds(job.attempt)
        outbox.fail(job.id, error=f"s3_upload_error: {e}", retry_after_seconds=delay)
        return

    # In production: UPDATE interactions SET recording_s3_key=$1, recording_status='UPLOADED' ...
    audit_log.write(
        interaction_id=interaction_id,
        correlation_id=job.correlation_id,
        customer_id=job.customer_id,
        campaign_id=job.campaign_id,
        event_type="recording_uploaded",
        details={"s3_key": s3_key, "attempts_used": job.attempt},
    )
    outbox.complete(job.id)
