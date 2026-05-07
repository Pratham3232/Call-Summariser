"""
Unified job runner / dispatcher.

Single entry point that picks the next claimable job from the outbox and
routes it to the appropriate handler:
    - postcall_analysis  -> analysis_worker.process_analysis_job
    - recording_upload   -> recording_poller.process_recording_job
    - signal_dispatch    -> signal_jobs.process_signal_dispatch_job
    - lead_stage_update  -> signal_jobs.process_lead_stage_job
    - crm_push           -> signal_jobs.process_crm_push_job

The runner is intentionally simple and deterministic so it can be:
    - Embedded in a Celery worker (one job per Celery task) — keeps the
      existing infra layout but makes the broker incidental.
    - Run as a long-lived loop (`run_forever`) inside a regular Python
      worker — useful when we want to drop Celery entirely.

Lane selection:
    Hot lane is preferred. The runner first tries to claim a hot job; if
    none is runnable it falls back to cold. This implements priority
    without separate brokers.

Worker fault tolerance:
    Visibility timeouts on the outbox row mean a worker crash mid-job
    causes another worker to reclaim the same job after timeout. Combined
    with idempotent handlers, this gives at-least-once delivery with
    exactly-once effect.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Dict, Optional

from src.services.analysis_worker import process_analysis_job
from src.services.audit_log import audit_log
from src.services.outbox import outbox, Job
from src.services.recording_poller import process_recording_job
from src.services.signal_handlers import (
    process_crm_push_job,
    process_lead_stage_job,
    process_signal_dispatch_job,
)

logger = logging.getLogger(__name__)


HANDLERS: Dict[str, Callable[[Job], Awaitable[None]]] = {
    "postcall_analysis": process_analysis_job,
    "recording_upload": process_recording_job,
    "signal_dispatch": process_signal_dispatch_job,
    "lead_stage_update": process_lead_stage_job,
    "crm_push": process_crm_push_job,
}


def _worker_id() -> str:
    return f"worker-{os.getpid()}-{id(asyncio.current_task() or 0)}"


async def run_one(lane: Optional[str] = None) -> bool:
    """
    Drain a single job. Returns True if a job ran (regardless of outcome),
    False if no runnable job was found.
    """
    job = outbox.claim_next(lane=lane, worker_id=_worker_id())
    if job is None and lane == "hot":
        # Hot lane empty — try cold.
        job = outbox.claim_next(lane="cold", worker_id=_worker_id())
    elif job is None and lane is None:
        return False
    if job is None:
        return False

    handler = HANDLERS.get(job.job_type)
    if handler is None:
        outbox.fail(job.id, error=f"no_handler_for_{job.job_type}")
        return True

    try:
        await handler(job)
    except Exception as e:
        # Defensive — handlers should manage their own outcomes, but if one
        # leaks an exception we still mark the job FAILED so the visibility
        # timeout doesn't have to expire.
        logger.exception("job_handler_crashed", extra={"job_id": job.id})
        outbox.fail(job.id, error=f"handler_crashed: {e}")
        audit_log.write(
            interaction_id=job.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=job.customer_id,
            campaign_id=job.campaign_id,
            event_type="handler_crashed",
            severity="ERROR",
            details={"job_id": job.id, "job_type": job.job_type, "error": str(e)},
        )
    return True


async def drain(max_jobs: int = 1000, lane: Optional[str] = None) -> int:
    """
    Run until the outbox is empty or `max_jobs` have been processed.
    Used by tests; production uses `run_forever`.
    """
    n = 0
    while n < max_jobs:
        ran = await run_one(lane=lane)
        if not ran:
            break
        n += 1
    return n


async def run_forever(
    poll_interval_seconds: float = 0.1,
    lane: Optional[str] = None,
):  # pragma: no cover — long-running loop
    """
    Long-lived dispatcher. SIGTERM-friendly; finishes the current job
    before exiting. Intended to be supervised (k8s / systemd / docker).
    """
    logger.info("job_runner_started", extra={"lane": lane})
    while True:
        ran = await run_one(lane=lane)
        if not ran:
            await asyncio.sleep(poll_interval_seconds)
