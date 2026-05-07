"""
Post-call analysis worker (v2).

Differences from v1's PostCallProcessor:
    1. Triage runs first. If the heuristic is high-confidence, we skip the
       LLM entirely (cold lane no-op outcomes).
    2. Before any LLM call we acquire budget via `budget_service`. If the
       budget says DEFER, the job is requeued without consuming an attempt.
       If it says REJECT, the job is dead-lettered with a clear reason.
    3. Every step writes an audit row.
    4. Result writes are split: the canonical `analysis_results` row is
       upserted (idempotent on interaction_id), and `interaction_metadata`
       is patched for the dashboard. A retry that runs after the LLM
       already succeeded is a no-op.
    5. Token usage is recorded against the customer for both billing
       (Postgres ledger) and real-time visibility (Redis sliding window).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from src.config import settings
from src.services.audit_log import audit_log
from src.services.budget import budget_service
from src.services.outbox import Job, outbox
from src.services.post_call_processor import (
    AnalysisResult,
    PostCallContext,
    PostCallProcessor,
)
from src.services.triage import triage, TriageDecision
from src.services.usage_tracker import usage_tracker

logger = logging.getLogger(__name__)


# In-memory canonical store for tests. Production = analysis_results table.
class _AnalysisStore:
    def __init__(self):
        self._rows: Dict[str, Dict[str, Any]] = {}

    def upsert(self, interaction_id: str, row: Dict[str, Any]):
        self._rows[interaction_id] = row

    def get(self, interaction_id: str) -> Optional[Dict[str, Any]]:
        return self._rows.get(interaction_id)

    def reset(self):
        self._rows.clear()


analysis_store = _AnalysisStore()


def _ctx_from_payload(payload: Dict[str, Any]) -> PostCallContext:
    return PostCallContext(
        interaction_id=payload["interaction_id"],
        session_id=payload.get("session_id", ""),
        lead_id=payload.get("lead_id", ""),
        campaign_id=payload.get("campaign_id", ""),
        customer_id=payload.get("customer_id", ""),
        agent_id=payload.get("agent_id", ""),
        call_sid=payload.get("call_sid", ""),
        transcript_text=payload.get("transcript_text", ""),
        conversation_data=payload.get("conversation_data", {}),
        additional_data=payload.get("additional_data", {}),
        ended_at=datetime.fromisoformat(payload["ended_at"])
        if payload.get("ended_at") else datetime.utcnow(),
        exotel_account_id=payload.get("exotel_account_id"),
    )


async def process_analysis_job(job: Job) -> None:
    """
    Idempotent analysis runner. The outbox guarantees at-least-once delivery;
    this function ensures exactly-once *effect* by:
      a) Bailing out early if `analysis_store` already has a row.
      b) Using upsert semantics on the result write.
    """
    if analysis_store.get(job.interaction_id):
        # Already processed — most likely a visibility-timeout reclaim after
        # the worker completed but before it could mark the job SUCCEEDED.
        outbox.complete(job.id)
        audit_log.write(
            interaction_id=job.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=job.customer_id,
            campaign_id=job.campaign_id,
            event_type="analysis_skipped_already_done",
            details={"job_id": job.id},
        )
        return

    ctx = _ctx_from_payload(job.payload)
    transcript = ctx.conversation_data.get("transcript", [])

    # ── Step 1: Triage ───────────────────────────────────────────────────
    decision: TriageDecision = triage(transcript, ctx.additional_data)
    audit_log.write(
        interaction_id=ctx.interaction_id,
        correlation_id=job.correlation_id,
        customer_id=ctx.customer_id,
        campaign_id=ctx.campaign_id,
        event_type="triaged",
        details={
            "lane": decision.lane,
            "label": decision.label,
            "confidence": decision.confidence,
            "needs_llm": decision.needs_llm,
            "matched": decision.matched_patterns,
        },
    )

    if not decision.needs_llm:
        # Heuristic-only result. Cold lane / clear outcome / short call —
        # the answer is deterministic, no LLM tokens are consumed.
        analysis_store.upsert(
            ctx.interaction_id,
            {
                "interaction_id": ctx.interaction_id,
                "customer_id": ctx.customer_id,
                "campaign_id": ctx.campaign_id,
                "triage_label": decision.label,
                "triage_confidence": decision.confidence,
                "call_stage": decision.label,
                "entities": {},
                "summary": "",
                "raw_response": {},
                "tokens_used": 0,
                "latency_ms": 0,
                "provider": "heuristic",
                "model": "triage-v1",
                "source": "heuristic",
            },
        )
        audit_log.write(
            interaction_id=ctx.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=ctx.customer_id,
            campaign_id=ctx.campaign_id,
            event_type="analysis_completed_heuristic",
            details={"call_stage": decision.label},
        )
        outbox.complete(job.id)
        return

    # ── Step 2: Budget check ─────────────────────────────────────────────
    estimated_tokens = settings.LLM_AVG_TOKENS_PER_CALL
    budget = await budget_service.acquire(
        customer_id=ctx.customer_id, estimated_tokens=estimated_tokens
    )

    if budget.decision == "DEFER":
        outbox.defer(
            job.id,
            retry_after_seconds=budget.retry_after_ms / 1000.0,
            reason=budget.reason,
        )
        audit_log.write(
            interaction_id=ctx.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=ctx.customer_id,
            campaign_id=ctx.campaign_id,
            event_type="budget_deferred",
            details={
                "reason": budget.reason,
                "retry_after_ms": budget.retry_after_ms,
            },
        )
        return

    if budget.decision == "REJECT":
        outbox.fail(job.id, error=f"budget_rejected: {budget.reason}")
        audit_log.write(
            interaction_id=ctx.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=ctx.customer_id,
            campaign_id=ctx.campaign_id,
            event_type="budget_rejected",
            severity="ERROR",
            details={"reason": budget.reason},
        )
        return

    # ── Step 3: LLM call ─────────────────────────────────────────────────
    audit_log.write(
        interaction_id=ctx.interaction_id,
        correlation_id=job.correlation_id,
        customer_id=ctx.customer_id,
        campaign_id=ctx.campaign_id,
        event_type="llm_started",
        details={"bucket": budget.bucket, "estimated_tokens": estimated_tokens},
    )

    processor = PostCallProcessor()
    try:
        result: AnalysisResult = await processor.process_post_call(ctx, single_prompt=True)
    except Exception as e:
        # If this is a 429 (rate-limited despite our limiter — happens if the
        # provider's account-wide limit is shared with other apps), defer
        # rather than burn an attempt.
        msg = str(e).lower()
        if "rate" in msg or "429" in msg:
            outbox.defer(job.id, retry_after_seconds=5.0, reason="provider_429")
            audit_log.write(
                interaction_id=ctx.interaction_id,
                correlation_id=job.correlation_id,
                customer_id=ctx.customer_id,
                campaign_id=ctx.campaign_id,
                event_type="llm_provider_429",
                severity="WARN",
                details={"error": str(e)},
            )
            return
        outbox.fail(job.id, error=f"llm_error: {e}")
        audit_log.write(
            interaction_id=ctx.interaction_id,
            correlation_id=job.correlation_id,
            customer_id=ctx.customer_id,
            campaign_id=ctx.campaign_id,
            event_type="llm_failed",
            severity="ERROR",
            details={"error": str(e)},
        )
        return

    # Correct any over/under-debit using the actual token count.
    drift = result.tokens_used - estimated_tokens
    budget_service.record_actual_tokens(ctx.customer_id, drift)
    await usage_tracker.record(
        interaction_id=ctx.interaction_id,
        customer_id=ctx.customer_id,
        campaign_id=ctx.campaign_id,
        tokens=result.tokens_used,
        provider=result.provider,
        model=result.model,
    )

    # Idempotent result write.
    analysis_store.upsert(
        ctx.interaction_id,
        {
            "interaction_id": ctx.interaction_id,
            "customer_id": ctx.customer_id,
            "campaign_id": ctx.campaign_id,
            "triage_label": decision.label,
            "triage_confidence": decision.confidence,
            "call_stage": result.call_stage,
            "entities": result.entities,
            "summary": result.summary,
            "raw_response": result.raw_response,
            "tokens_used": result.tokens_used,
            "latency_ms": result.latency_ms,
            "provider": result.provider,
            "model": result.model,
            "source": "llm",
        },
    )

    audit_log.write(
        interaction_id=ctx.interaction_id,
        correlation_id=job.correlation_id,
        customer_id=ctx.customer_id,
        campaign_id=ctx.campaign_id,
        event_type="analysis_completed_llm",
        details={
            "call_stage": result.call_stage,
            "tokens_used": result.tokens_used,
            "latency_ms": result.latency_ms,
            "bucket": budget.bucket,
        },
    )

    # ── Step 4: Enqueue downstream jobs ──────────────────────────────────
    # Signal jobs and lead-stage updates run as separate durable jobs so a
    # CRM webhook outage doesn't roll back the analysis result.
    outbox.enqueue(
        interaction_id=ctx.interaction_id,
        customer_id=ctx.customer_id,
        campaign_id=ctx.campaign_id,
        correlation_id=job.correlation_id,
        job_type="signal_dispatch",
        lane=job.lane,
        payload={
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "lead_id": ctx.lead_id,
            "campaign_id": ctx.campaign_id,
            "call_stage": result.call_stage,
            "entities": result.entities,
        },
    )

    outbox.complete(job.id)
