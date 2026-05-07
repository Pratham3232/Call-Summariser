"""
FastAPI endpoint for ending an interaction (v2).

POST /session/{session_id}/interaction/{interaction_id}/end

Contract changes vs v1 (justified in SUBMISSION.md §12):
    - Response now includes `correlation_id` and `lane`. Callers can use
      these to follow the call through audit logs.
    - The endpoint NO LONGER fires signal_jobs from asyncio.create_task.
      All downstream effects come from durable jobs in the outbox.

Flow:
    1. Validate request, generate correlation_id.
    2. Run cheap triage (no LLM) to choose lane.
    3. Persist the call-end facts: status=ENDED, lane, triage_label
       (in production this is a single transactional UPDATE).
    4. Enqueue durable jobs:
         a) `recording_upload`  — independent poller, exponential backoff.
         b) `postcall_analysis` — picked up by the lane's worker pool.
       Short calls (`lane=='skip'`) get an immediate heuristic-only
       analysis job that completes without an LLM hit.
    5. Return 200 with the correlation_id.

Why this is faster *and* safer:
    - Recording is no longer in the critical path of the response.
    - The endpoint never blocks on Redis or Celery health — outbox
      enqueue is a single Postgres INSERT.
    - The dashboard sees `analysis_status=pending` immediately rather
      than the v1 placeholder "processing" sent via empty signal jobs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.services.audit_log import audit_log
from src.services.outbox import outbox
from src.services.triage import triage

logger = logging.getLogger(__name__)
router = APIRouter()


class InteractionEndRequest(BaseModel):
    call_sid: Optional[str] = None
    duration_seconds: Optional[int] = None
    call_status: Optional[str] = None
    additional_data: Optional[Dict[str, Any]] = None


class InteractionEndResponse(BaseModel):
    status: str
    interaction_id: str
    correlation_id: str
    lane: str
    triage_label: str
    message: str


@router.post(
    "/session/{session_id}/interaction/{interaction_id}/end",
    response_model=InteractionEndResponse,
)
async def end_interaction(
    session_id: UUID,
    interaction_id: UUID,
    request: InteractionEndRequest,
):
    try:
        interaction = await _load_interaction(interaction_id)
        if not interaction:
            raise HTTPException(status_code=404, detail="Interaction not found")

        correlation_id = str(uuid.uuid4())
        customer_id = interaction["customer_id"]
        campaign_id = interaction["campaign_id"]
        transcript = (interaction.get("conversation_data") or {}).get("transcript", [])

        # Pre-LLM triage decides the lane before we enqueue anything.
        decision = triage(transcript, request.additional_data or {})
        lane = decision.lane

        await _update_interaction_status(
            interaction_id=str(interaction_id),
            status="ENDED",
            lane=lane,
            triage_label=decision.label,
            correlation_id=correlation_id,
            ended_at=datetime.utcnow(),
            duration=request.duration_seconds,
            call_sid=request.call_sid,
        )

        audit_log.write(
            interaction_id=str(interaction_id),
            correlation_id=correlation_id,
            customer_id=customer_id,
            campaign_id=campaign_id,
            event_type="call_ended",
            details={
                "session_id": str(session_id),
                "duration_seconds": request.duration_seconds,
                "transcript_turns": len(transcript),
                "triage_lane": lane,
                "triage_label": decision.label,
                "triage_confidence": decision.confidence,
            },
        )

        # ── Recording: independent durable job (no 45s sleep) ────────────
        if request.call_sid and interaction.get("exotel_account_id"):
            outbox.enqueue(
                interaction_id=str(interaction_id),
                customer_id=customer_id,
                campaign_id=campaign_id,
                correlation_id=correlation_id,
                job_type="recording_upload",
                lane=lane if lane != "skip" else "cold",
                payload={
                    "interaction_id": str(interaction_id),
                    "call_sid": request.call_sid,
                    "exotel_account_id": interaction["exotel_account_id"],
                },
                # The recording poller's first attempt should be quick — Exotel
                # is usually ready in 10-30s, so we delay only minimally.
                delay_seconds=0.0,
            )

        # ── Analysis: durable job, lane-aware ────────────────────────────
        # `skip` lane = short transcript: still enqueue the analysis job so
        # the heuristic outcome is recorded transactionally + audited. This
        # is what fixes the v1 bug where `asyncio.create_task` could vanish.
        analysis_lane = "cold" if lane == "skip" else lane
        transcript_text = "\n".join(
            f"{turn.get('role', 'unknown')}: {turn.get('content', '')}"
            for turn in transcript
        )

        outbox.enqueue(
            interaction_id=str(interaction_id),
            customer_id=customer_id,
            campaign_id=campaign_id,
            correlation_id=correlation_id,
            job_type="postcall_analysis",
            lane=analysis_lane,
            payload={
                "interaction_id": str(interaction_id),
                "session_id": str(session_id),
                "lead_id": interaction["lead_id"],
                "campaign_id": campaign_id,
                "customer_id": customer_id,
                "agent_id": interaction["agent_id"],
                "call_sid": request.call_sid,
                "transcript_text": transcript_text,
                "conversation_data": interaction.get("conversation_data", {}),
                "additional_data": request.additional_data or {},
                "ended_at": datetime.utcnow().isoformat(),
                "exotel_account_id": interaction.get("exotel_account_id"),
            },
        )

        return InteractionEndResponse(
            status="ok",
            interaction_id=str(interaction_id),
            correlation_id=correlation_id,
            lane=lane,
            triage_label=decision.label,
            message="Interaction ended, processing scheduled",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "end_interaction_failed",
            extra={"interaction_id": str(interaction_id), "error": str(e)},
        )
        raise HTTPException(status_code=500, detail="Internal server error")


async def _load_interaction(interaction_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Production: SELECT * FROM interactions WHERE id = $1.
    Mock used by the assessment. Customer/campaign IDs match the seed
    rows in data/schema.sql so they line up with the registered
    customer reservations.
    """
    return {
        "id": str(interaction_id),
        "lead_id": "a0000000-0000-0000-0000-000000000001",
        "campaign_id": "c0000000-0000-0000-0000-000000000001",
        "customer_id": "d0000000-0000-0000-0000-000000000001",
        "agent_id": "e0000000-0000-0000-0000-000000000001",
        "exotel_account_id": "mock-exotel-account",
        "conversation_data": {
            "transcript": [
                {"role": "agent", "content": "Hello, am I speaking with Mr. Sharma?"},
                {"role": "customer", "content": "Yes, speaking."},
                {"role": "agent", "content": "I'm calling from XYZ about your recent inquiry."},
                {"role": "customer", "content": "Oh yes, I was looking at the product."},
                {"role": "agent", "content": "Would you like to schedule a demo?"},
                {"role": "customer", "content": "Sure, let's do tomorrow at 3 PM."},
                {"role": "agent", "content": "Perfect, I've booked a demo for tomorrow at 3 PM."},
                {"role": "customer", "content": "Thank you, bye."},
            ]
        },
    }


async def _update_interaction_status(
    *,
    interaction_id: str,
    status: str,
    lane: str,
    triage_label: str,
    correlation_id: str,
    ended_at: datetime,
    duration: Optional[int],
    call_sid: Optional[str],
) -> None:
    """
    Production:
        UPDATE interactions
        SET status = $1, ended_at = $2, duration_seconds = $3,
            call_sid = $4, lane = $5, triage_label = $6,
            correlation_id = $7, analysis_status = 'pending'
        WHERE id = $interaction_id;
    """
    logger.info(
        "interaction_status_updated",
        extra={
            "interaction_id": interaction_id,
            "status": status,
            "lane": lane,
            "triage_label": triage_label,
            "correlation_id": correlation_id,
            "ended_at": ended_at.isoformat(),
        },
    )
