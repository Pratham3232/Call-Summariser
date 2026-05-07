"""
Durable handlers for the post-analysis fan-out: signal dispatch, lead-stage
updates, CRM push.

Each one is a Job handler — invoked by the runner with a claimed `Job` and
responsible for managing its own outcome via outbox.complete()/fail().

These were fire-and-forget asyncio.create_tasks in v1. v2 makes them
durable jobs with retries + DLQ, so a CRM webhook flap doesn't drop the
"this lead booked a demo" payload.
"""

from __future__ import annotations

import logging

from src.services.audit_log import audit_log
from src.services.outbox import Job, outbox
from src.services.signal_jobs import trigger_signal_jobs, update_lead_stage

logger = logging.getLogger(__name__)


async def process_signal_dispatch_job(job: Job) -> None:
    payload = job.payload
    try:
        await trigger_signal_jobs(
            interaction_id=job.interaction_id,
            session_id=payload.get("session_id", ""),
            campaign_id=job.campaign_id,
            analysis_result={
                "call_stage": payload.get("call_stage"),
                "entities": payload.get("entities", {}),
            },
        )
    except Exception as e:
        outbox.fail(job.id, error=f"signal_dispatch_error: {e}")
        return

    # After a successful dispatch we enqueue the lead-stage update + CRM
    # push as separate jobs. They have independent retry budgets — a CRM
    # outage doesn't block the lead-stage update from firing.
    outbox.enqueue(
        interaction_id=job.interaction_id,
        customer_id=job.customer_id,
        campaign_id=job.campaign_id,
        correlation_id=job.correlation_id,
        job_type="lead_stage_update",
        lane=job.lane,
        payload={
            "lead_id": payload.get("lead_id", ""),
            "interaction_id": job.interaction_id,
            "call_stage": payload.get("call_stage", ""),
        },
    )
    outbox.enqueue(
        interaction_id=job.interaction_id,
        customer_id=job.customer_id,
        campaign_id=job.campaign_id,
        correlation_id=job.correlation_id,
        job_type="crm_push",
        lane="cold",  # CRM push is rarely time-critical even for hot calls.
        payload={
            "interaction_id": job.interaction_id,
            "call_stage": payload.get("call_stage", ""),
            "entities": payload.get("entities", {}),
        },
    )

    audit_log.write(
        interaction_id=job.interaction_id,
        correlation_id=job.correlation_id,
        customer_id=job.customer_id,
        campaign_id=job.campaign_id,
        event_type="signal_dispatched",
        details={"call_stage": payload.get("call_stage")},
    )
    outbox.complete(job.id)


async def process_lead_stage_job(job: Job) -> None:
    payload = job.payload
    try:
        await update_lead_stage(
            lead_id=payload.get("lead_id", ""),
            interaction_id=job.interaction_id,
            call_stage=payload.get("call_stage", ""),
        )
    except Exception as e:
        outbox.fail(job.id, error=f"lead_stage_error: {e}")
        return

    audit_log.write(
        interaction_id=job.interaction_id,
        correlation_id=job.correlation_id,
        customer_id=job.customer_id,
        campaign_id=job.campaign_id,
        event_type="lead_stage_updated",
        details={"call_stage": payload.get("call_stage")},
    )
    outbox.complete(job.id)


async def process_crm_push_job(job: Job) -> None:
    """
    CRM push — webhook to the customer's configured endpoint.

    This is the canonical place to add per-customer config (auth headers,
    URL, retry profile). For the assessment we mock the call and treat
    success as the default; failures hit the standard outbox retry path.
    """
    audit_log.write(
        interaction_id=job.interaction_id,
        correlation_id=job.correlation_id,
        customer_id=job.customer_id,
        campaign_id=job.campaign_id,
        event_type="crm_push_attempted",
        details={"call_stage": job.payload.get("call_stage")},
    )
    # Mock success for the assessment; production runs an httpx POST with
    # signed headers and customer-specific TLS pinning.
    outbox.complete(job.id)
