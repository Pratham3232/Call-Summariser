"""
Celery tasks (v2).

In v2, Celery is no longer the system of record for in-flight work — the
Postgres `interaction_jobs` outbox is. Celery is now an *executor*: a
mechanism that wakes up a worker and asks the runner to pull the next
claimable job. This means:

    - A broker restart loses zero work; the outbox still has it.
    - Visibility timeouts on jobs handle worker crashes uniformly,
      whether the worker died before, during, or after the broker call.
    - Replacing Celery with k8s CronJobs / a long-lived runner is a
      one-line change (`run_forever()` from job_runner.py).

Two task entry points:

    1. `process_interaction_end_background_task(payload)` — kept for
       backwards-compatibility with any caller still using the v1 API.
       It now translates the v1 payload into outbox jobs and immediately
       drains them. New code should use the outbox directly.

    2. `dispatch_jobs(lane=None, max_jobs=N)` — the v2 task. A scheduler
       (Celery beat / k8s CronJob) calls this every few seconds; it
       claims and processes whatever the outbox has runnable.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from src.services.audit_log import audit_log
from src.services.job_runner import drain
from src.services.outbox import outbox
from src.services.triage import triage
from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="process_interaction_end_background_task",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def process_interaction_end_background_task(self, payload: Dict[str, Any]):
    """
    v1 compatibility shim.

    Accepts the v1 payload, computes the lane, enqueues durable jobs,
    and drains a few. Idempotency keys ensure that if Celery redelivers
    after a broker restart we don't enqueue duplicates.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_enqueue_and_drain(payload))
    finally:
        loop.close()


async def _enqueue_and_drain(payload: Dict[str, Any]) -> None:
    interaction_id = payload["interaction_id"]
    customer_id = payload["customer_id"]
    campaign_id = payload["campaign_id"]
    correlation_id = payload.get("correlation_id") or str(uuid.uuid4())

    transcript = (payload.get("conversation_data") or {}).get("transcript", [])
    decision = triage(transcript, payload.get("additional_data") or {})
    lane = "cold" if decision.lane == "skip" else decision.lane

    if payload.get("call_sid") and payload.get("exotel_account_id"):
        outbox.enqueue(
            interaction_id=interaction_id,
            customer_id=customer_id,
            campaign_id=campaign_id,
            correlation_id=correlation_id,
            job_type="recording_upload",
            lane=lane,
            payload={
                "interaction_id": interaction_id,
                "call_sid": payload["call_sid"],
                "exotel_account_id": payload["exotel_account_id"],
            },
        )

    outbox.enqueue(
        interaction_id=interaction_id,
        customer_id=customer_id,
        campaign_id=campaign_id,
        correlation_id=correlation_id,
        job_type="postcall_analysis",
        lane=lane,
        payload={**payload, "ended_at": payload.get("ended_at") or datetime.utcnow().isoformat()},
    )

    audit_log.write(
        interaction_id=interaction_id,
        correlation_id=correlation_id,
        customer_id=customer_id,
        campaign_id=campaign_id,
        event_type="celery_v1_compat_enqueue",
        details={"lane": lane, "triage_label": decision.label},
    )

    # Drain a small batch synchronously so the v1 expectation of "Celery
    # processed something" still holds. Long-tail work is handled by the
    # dispatcher task below.
    await drain(max_jobs=10)


@celery_app.task(name="postcall.dispatch_jobs", bind=True, acks_late=True)
def dispatch_jobs(self, lane: Optional[str] = None, max_jobs: int = 50):
    """
    Lane-aware job dispatcher. Schedule via Celery beat, e.g.:

        celery_app.conf.beat_schedule = {
            "dispatch-hot": {
                "task": "postcall.dispatch_jobs",
                "schedule": 1.0,                     # every 1s
                "kwargs": {"lane": "hot",  "max_jobs": 50},
            },
            "dispatch-cold": {
                "task": "postcall.dispatch_jobs",
                "schedule": 5.0,                     # every 5s
                "kwargs": {"lane": "cold", "max_jobs": 200},
            },
        }
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(drain(max_jobs=max_jobs, lane=lane))
    finally:
        loop.close()
