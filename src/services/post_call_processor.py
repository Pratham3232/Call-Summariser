"""
PostCallProcessor — pure LLM-call mechanism.

In v2 this class is a thin "make the LLM request, parse the response"
helper. All scheduling, budgeting, triage, retry and audit decisions live
in `analysis_worker.py`. Keeping the LLM-call concerns isolated here means
the worker can be reasoned about (and tested) without mocking provider
HTTP, and the PostCallProcessor can be reused by ad-hoc tooling
(replay scripts, ops queries) without dragging in the whole pipeline.

Differences from v1:
  - Removed the `circuit_breaker.record_postcall_start/end` calls. The
    in-flight RPM counter they fed has been replaced by the token-bucket
    rate limiter, which checks budget BEFORE the request rather than
    after. The class no longer has any side effects beyond the LLM call
    and the dashboard metadata write.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from dataclasses import dataclass

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class PostCallContext:
    """Everything needed to process one completed call."""
    interaction_id: str
    session_id: str
    lead_id: str
    campaign_id: str
    customer_id: str  # The business using the platform (not the person called)
    agent_id: str
    call_sid: str     # Exotel's identifier for the call
    transcript_text: str
    conversation_data: dict
    additional_data: dict  # Arbitrary metadata from the dialler (campaign config, etc.)
    ended_at: datetime
    exotel_account_id: Optional[str] = None


@dataclass
class AnalysisResult:
    call_stage: str          # Disposition: rebook_confirmed, not_interested, etc.
    entities: Dict[str, Any] # Structured entities extracted from the transcript
    summary: str             # Human-readable summary for dashboard display
    raw_response: Dict[str, Any]
    tokens_used: int         # Actual tokens consumed — source of truth for billing
    latency_ms: float
    provider: str
    model: str


class PostCallProcessor:
    """
    Pure LLM-call mechanism. All scheduling/budget/audit policy lives in
    `analysis_worker`. This class only knows how to:
      1. Build the prompt
      2. Call the LLM
      3. Parse the response
      4. Patch the dashboard metadata cache
    """

    async def process_post_call(
        self, ctx: PostCallContext, single_prompt: bool = True
    ) -> AnalysisResult:
        prompt = self._build_analysis_prompt(
            ctx.transcript_text,
            ctx.additional_data,
            single_prompt,
        )

        start_time = datetime.now(timezone.utc)
        response = await self._call_llm(prompt)
        elapsed_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

        result = self._parse_response(response, elapsed_ms)

        await self._update_interaction_metadata(ctx.interaction_id, result)

        logger.info(
            "postcall_analysis_complete",
            extra={
                "interaction_id": ctx.interaction_id,
                "customer_id": ctx.customer_id,
                "campaign_id": ctx.campaign_id,
                "call_stage": result.call_stage,
                "tokens_used": result.tokens_used,
                "latency_ms": result.latency_ms,
            },
        )

        return result

    def _build_analysis_prompt(
        self,
        transcript: str,
        additional_data: dict,
        single_prompt: bool,
    ) -> str:
        """
        Build the LLM prompt.

        The system prompt asks for three outputs in one JSON object.
        call_stage is the most important — everything downstream depends on it.
        entities and summary are useful but secondary.

        If you were thinking about a cheaper "just classify the call_stage" step
        before the full analysis, this is the prompt you'd be splitting.
        """
        system_prompt = """You are a call analysis assistant. Analyze the following
call transcript and extract:
1. call_stage: The outcome/disposition of the call
2. entities: Key information mentioned (dates, times, amounts, names, preferences)
3. summary: A brief summary of what happened in the call

Respond in JSON format:
{
    "call_stage": "...",
    "entities": {...},
    "summary": "..."
}"""

        return (
            f"{system_prompt}\n\n"
            f"Transcript:\n{transcript}\n\n"
            f"Additional context:\n{json.dumps(additional_data)}"
        )

    async def _call_llm(self, prompt: str) -> dict:
        """
        Call the configured LLM provider.

        In production this is an httpx POST to the provider's API.
        A 429 response raises an exception that propagates up to the Celery
        retry handler — which retries after a fixed 60-second delay regardless
        of the Retry-After header the provider sends back.

        Mock implementation for the assessment.
        """
        # The provider's response includes a `usage` block:
        # {"prompt_tokens": N, "completion_tokens": M, "total_tokens": N+M}
        # We surface total_tokens in AnalysisResult but don't write it back
        # anywhere that could be used for budget tracking or alerting.
        return {
            "call_stage": "unknown",
            "entities": {},
            "summary": "Mock analysis result",
            "usage": {"total_tokens": 1500},
        }

    def _parse_response(self, response: dict, latency_ms: float) -> AnalysisResult:
        return AnalysisResult(
            call_stage=response.get("call_stage", "unknown"),
            entities=response.get("entities", {}),
            summary=response.get("summary", ""),
            raw_response=response,
            tokens_used=response.get("usage", {}).get("total_tokens", 0),
            latency_ms=latency_ms,
            provider=settings.LLM_PROVIDER,
            model=settings.LLM_MODEL,
        )

    async def _update_interaction_metadata(
        self, interaction_id: str, result: AnalysisResult
    ) -> None:
        """
        Write analysis results into the interaction_metadata JSONB column.

        In production:
            UPDATE interactions
            SET interaction_metadata = interaction_metadata || $2::jsonb,
                updated_at = NOW()
            WHERE id = $1

        The dashboard reads interaction_metadata directly. There is no separate
        results table — this JSONB column is the only record of the analysis.
        If it gets overwritten by a retry, the previous result is gone.
        """
        logger.info(
            "metadata_updated",
            extra={
                "interaction_id": interaction_id,
                "call_stage": result.call_stage,
            },
        )


post_call_processor = PostCallProcessor()
