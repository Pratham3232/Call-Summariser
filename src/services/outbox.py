"""
Durable job outbox.

Replaces "implicit Celery state in Redis" with a queryable Postgres table
that is the source of truth for in-flight work. Celery / any worker
consumes from this table; if the broker dies, jobs are still here.

State machine:

      ┌────────┐  enqueue                ┌──────────┐
      │ caller │ ─────────────────────►  │ PENDING  │
      └────────┘                         └────┬─────┘
                                              │ claim()
                                              ▼
                                        ┌──────────┐
                                        │ CLAIMED  │ (visibility timeout)
                                        └────┬─────┘
                       complete()  ┌─────────┴─────────┐  fail()
                                   ▼                   ▼
                              ┌─────────┐       ┌──────────┐
                              │SUCCEEDED│       │ FAILED    │
                              └─────────┘       └────┬──────┘
                                                    │ attempt < max
                                                    ▼
                                            (back to PENDING with backoff)
                                                    │ attempt >= max
                                                    ▼
                                              ┌──────────────┐
                                              │ DEAD_LETTERED │
                                              └──────────────┘

Idempotency:
    `idempotency_key` is unique; enqueue() with an existing key is a no-op.
    Workers wrap business logic in an idempotent transaction (typically
    "upsert analysis_results, then update the job row"), so even if a
    timeout-then-reclaim happens, double-application is harmless.

For the assessment this module is a Postgres-shaped in-memory store —
the SQL is documented next to each method and is the same shape we'd
issue against `interaction_jobs`. Tests use the in-memory implementation
directly; integration with a real DB is straightforward to swap in.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from src.config import settings
from src.services.audit_log import audit_log
from src.services.dlq import dlq_store

logger = logging.getLogger(__name__)


JOB_STATES = {"PENDING", "CLAIMED", "SUCCEEDED", "FAILED", "DEAD_LETTERED"}


@dataclass
class Job:
    id: str
    interaction_id: str
    customer_id: str
    campaign_id: str
    correlation_id: str
    job_type: str
    lane: str
    idempotency_key: str
    payload: Dict[str, Any]
    state: str = "PENDING"
    attempt: int = 0
    max_attempts: int = 8
    next_run_at: float = field(default_factory=time.time)
    claimed_until: Optional[float] = None
    claimed_by: Optional[str] = None
    last_error: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class JobOutbox:
    """
    In-memory job table. Postgres equivalents are documented per method.
    """

    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._by_idem: Dict[str, str] = {}
        self._lock = threading.Lock()

    # --- enqueue ------------------------------------------------------------

    def enqueue(
        self,
        *,
        interaction_id: str,
        customer_id: str,
        campaign_id: str,
        correlation_id: str,
        job_type: str,
        lane: str,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        delay_seconds: float = 0.0,
        max_attempts: Optional[int] = None,
    ) -> Job:
        """
        SQL:
            INSERT INTO interaction_jobs
                (id, interaction_id, customer_id, campaign_id, correlation_id,
                 job_type, lane, idempotency_key, payload, max_attempts, next_run_at)
            VALUES (...)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *;
        """
        idem = idempotency_key or f"{interaction_id}:{job_type}"
        with self._lock:
            existing_id = self._by_idem.get(idem)
            if existing_id:
                return self._jobs[existing_id]
            job = Job(
                id=str(uuid.uuid4()),
                interaction_id=interaction_id,
                customer_id=customer_id,
                campaign_id=campaign_id,
                correlation_id=correlation_id,
                job_type=job_type,
                lane=lane,
                idempotency_key=idem,
                payload=payload,
                next_run_at=time.time() + max(0.0, delay_seconds),
                max_attempts=max_attempts or settings.JOB_MAX_ATTEMPTS,
            )
            self._jobs[job.id] = job
            self._by_idem[idem] = job.id

        audit_log.write(
            interaction_id=interaction_id,
            correlation_id=correlation_id,
            customer_id=customer_id,
            campaign_id=campaign_id,
            event_type="job_enqueued",
            details={"job_type": job_type, "lane": lane, "job_id": job.id},
        )
        return job

    # --- claim --------------------------------------------------------------

    def claim_next(
        self,
        lane: Optional[str] = None,
        worker_id: str = "worker-1",
        visibility_timeout_seconds: Optional[int] = None,
    ) -> Optional[Job]:
        """
        Atomically pick the next runnable job and mark it CLAIMED with a
        visibility timeout. SQL:

            UPDATE interaction_jobs
            SET state = 'CLAIMED',
                claimed_until = NOW() + interval '...',
                claimed_by = $worker_id,
                attempt = attempt + 1,
                updated_at = NOW()
            WHERE id = (
                SELECT id FROM interaction_jobs
                WHERE state IN ('PENDING', 'FAILED')
                  AND ($lane IS NULL OR lane = $lane)
                  AND next_run_at <= NOW()
                  AND (claimed_until IS NULL OR claimed_until < NOW())
                ORDER BY lane = 'hot' DESC, next_run_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *;
        """
        timeout = (
            visibility_timeout_seconds
            if visibility_timeout_seconds is not None
            else settings.JOB_VISIBILITY_TIMEOUT_SECONDS
        )
        now = time.time()
        with self._lock:
            candidates = sorted(
                (
                    j for j in self._jobs.values()
                    # PENDING / FAILED are runnable.
                    # CLAIMED with an expired visibility timeout means the
                    # previous worker died — reclaim it.
                    if (
                        j.state in ("PENDING", "FAILED")
                        or (
                            j.state == "CLAIMED"
                            and j.claimed_until is not None
                            and j.claimed_until <= now
                        )
                    )
                    and (lane is None or j.lane == lane)
                    and j.next_run_at <= now
                ),
                # Hot lane first, then earliest scheduled.
                key=lambda j: (j.lane != "hot", j.next_run_at),
            )
            if not candidates:
                return None
            job = candidates[0]
            job.state = "CLAIMED"
            job.attempt += 1
            job.claimed_until = now + timeout
            job.claimed_by = worker_id

        audit_log.write(
            interaction_id=job.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=job.customer_id,
            campaign_id=job.campaign_id,
            event_type="job_claimed",
            details={"job_id": job.id, "attempt": job.attempt, "worker": worker_id},
        )
        return job

    # --- complete -----------------------------------------------------------

    def complete(self, job_id: str):
        """
        SQL:
            UPDATE interaction_jobs SET state='SUCCEEDED', updated_at=NOW()
            WHERE id = $1;
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.state = "SUCCEEDED"
            job.claimed_until = None
            job.claimed_by = None
            job.last_error = None

        audit_log.write(
            interaction_id=job.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=job.customer_id,
            campaign_id=job.campaign_id,
            event_type="job_succeeded",
            details={"job_id": job.id, "attempts_used": job.attempt},
        )

    # --- fail ---------------------------------------------------------------

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        retry_after_seconds: Optional[float] = None,
    ):
        """
        Fail-and-maybe-retry. If `attempt >= max_attempts`, the job is
        promoted to DEAD_LETTERED and copied to `dlq_jobs` so an operator
        can replay it.

        SQL (success path — schedule retry):
            UPDATE interaction_jobs
            SET state = 'FAILED',
                last_error = $err,
                next_run_at = NOW() + interval '...',
                claimed_until = NULL,
                updated_at = NOW()
            WHERE id = $1;

        SQL (DLQ path):
            UPDATE interaction_jobs
            SET state = 'DEAD_LETTERED', last_error = $err, updated_at = NOW()
            WHERE id = $1;
            INSERT INTO dlq_jobs (...) ...;
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.last_error = error
            job.claimed_until = None
            job.claimed_by = None

            if job.attempt >= job.max_attempts:
                job.state = "DEAD_LETTERED"
                dlq_store.add(
                    original_job_id=job.id,
                    interaction_id=job.interaction_id,
                    customer_id=job.customer_id,
                    job_type=job.job_type,
                    payload=job.payload,
                    last_error=error,
                    attempts=job.attempt,
                    correlation_id=job.correlation_id,
                )
                audit_log.write(
                    interaction_id=job.interaction_id,
                    correlation_id=job.correlation_id,
                    customer_id=job.customer_id,
                    campaign_id=job.campaign_id,
                    event_type="job_dead_lettered",
                    severity="ERROR",
                    message=error,
                    details={
                        "job_id": job.id,
                        "job_type": job.job_type,
                        "attempts_used": job.attempt,
                    },
                )
                return

            job.state = "FAILED"
            backoff = retry_after_seconds
            if backoff is None:
                # Exponential backoff with jitter cap: 2^attempt seconds, max 5 min.
                backoff = min(300.0, 2 ** job.attempt)
            job.next_run_at = time.time() + backoff

        audit_log.write(
            interaction_id=job.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=job.customer_id,
            campaign_id=job.campaign_id,
            event_type="job_failed",
            severity="WARN",
            message=error,
            details={
                "job_id": job.id,
                "attempt": job.attempt,
                "retry_in_s": round(job.next_run_at - time.time(), 2),
            },
        )

    # --- defer (rate-limited) ----------------------------------------------

    def defer(self, job_id: str, retry_after_seconds: float, reason: str):
        """
        Soft requeue — does NOT count against max_attempts. Used when the
        rate limiter says "wait", so a burst doesn't artificially exhaust
        a job's retry budget.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.state = "PENDING"
            job.attempt = max(0, job.attempt - 1)  # refund the attempt counter
            job.next_run_at = time.time() + max(0.5, retry_after_seconds)
            job.claimed_until = None
            job.claimed_by = None
            job.last_error = reason

        audit_log.write(
            interaction_id=job.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=job.customer_id,
            campaign_id=job.campaign_id,
            event_type="job_deferred",
            details={
                "job_id": job.id,
                "reason": reason,
                "retry_in_s": round(retry_after_seconds, 2),
            },
        )

    # --- introspection ------------------------------------------------------

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def find(
        self,
        interaction_id: Optional[str] = None,
        state: Optional[str] = None,
        job_type: Optional[str] = None,
    ) -> List[Job]:
        return [
            j for j in self._jobs.values()
            if (interaction_id is None or j.interaction_id == interaction_id)
            and (state is None or j.state == state)
            and (job_type is None or j.job_type == job_type)
        ]

    def reset(self):
        with self._lock:
            self._jobs.clear()
            self._by_idem.clear()

    def queue_depth(self, lane: Optional[str] = None) -> int:
        return len(
            [
                j for j in self._jobs.values()
                if j.state in ("PENDING", "FAILED")
                and (lane is None or j.lane == lane)
            ]
        )


outbox = JobOutbox()
