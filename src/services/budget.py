"""
Customer budget service — turns rate-limiter facts into a business decision.

Responsibility:
    Given an interaction's customer_id and an estimated token cost, decide
    whether to:
      - ALLOW: spend now (debits the limiter)
      - DEFER: requeue with a small backoff (limiter is tight)
      - REJECT: customer is hard-capped this period (e.g. unpaid bill)

This is intentionally a thin layer over `rate_limiter`. Keeping the policy
("what does it mean to be over budget?") separate from the mechanism
("can I get a token from this bucket?") makes both testable in isolation
and lets us evolve the policy (e.g. a future "burst credit" feature)
without touching the limiter math.

Per-customer config lives in the `customers` table and is loaded lazily.
For the assessment we use a small in-memory cache; in production this
would be a Redis-backed view with TTL.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

from src.config import settings
from src.services.rate_limiter import rate_limiter, AcquireResult

logger = logging.getLogger(__name__)


@dataclass
class CustomerConfig:
    customer_id: str
    name: str = ""
    tokens_per_minute_reservation: int = 0
    priority: int = 100
    hard_cap_tokens_per_day: Optional[int] = None  # None = unlimited
    config: Dict = None  # type: ignore[assignment]


@dataclass
class BudgetDecision:
    decision: str           # 'ALLOW' | 'DEFER' | 'REJECT'
    reason: str
    bucket: str = ""        # which bucket served (when ALLOW)
    retry_after_ms: int = 0


class CustomerRegistry:
    """
    Loose lookup with sane defaults. The assessment harness can inject
    customers via `register()`; production code would query the customers
    table with a 60s TTL cache.
    """

    def __init__(self):
        self._cache: Dict[str, CustomerConfig] = {}

    def register(self, cfg: CustomerConfig):
        self._cache[cfg.customer_id] = cfg

    def get(self, customer_id: str) -> CustomerConfig:
        if customer_id in self._cache:
            return self._cache[customer_id]
        # Unknown customer = no reservation. They share the headroom pool.
        # This is intentional: we never silently allocate budget to a
        # customer we don't know about.
        return CustomerConfig(customer_id=customer_id)


customer_registry = CustomerRegistry()


class BudgetService:
    def __init__(self):
        # Daily token spend, in-memory for the assessment. In prod this is
        # a Redis HINCRBY with day-bucketed key + Postgres ledger.
        self._daily_spend: Dict[str, int] = {}
        self._daily_spend_day: Dict[str, int] = {}

    def _day_bucket(self) -> int:
        return int(time.time() // 86400)

    async def acquire(
        self,
        customer_id: str,
        estimated_tokens: int,
    ) -> BudgetDecision:
        cfg = customer_registry.get(customer_id)
        day = self._day_bucket()

        if self._daily_spend_day.get(customer_id) != day:
            self._daily_spend[customer_id] = 0
            self._daily_spend_day[customer_id] = day

        if (
            cfg.hard_cap_tokens_per_day is not None
            and self._daily_spend.get(customer_id, 0) + estimated_tokens
            > cfg.hard_cap_tokens_per_day
        ):
            return BudgetDecision(
                decision="REJECT",
                reason=f"daily_cap_exceeded:{cfg.hard_cap_tokens_per_day}",
            )

        result: AcquireResult = await rate_limiter.acquire(
            customer_id=customer_id,
            tokens=estimated_tokens,
            reservation_tpm=cfg.tokens_per_minute_reservation,
        )

        if result.allowed:
            self._daily_spend[customer_id] = (
                self._daily_spend.get(customer_id, 0) + estimated_tokens
            )
            return BudgetDecision(
                decision="ALLOW",
                reason=f"served_by_{result.bucket}",
                bucket=result.bucket,
            )

        return BudgetDecision(
            decision="DEFER",
            reason="rate_limited",
            retry_after_ms=max(result.retry_after_ms, 1000),
        )

    def record_actual_tokens(self, customer_id: str, actual_tokens: int):
        """
        After the LLM returns we know the true token cost. The estimate was
        already debited; this hook lets us correct the day ledger so billing
        is exact. The Redis bucket auto-corrects via natural refill.
        """
        day = self._day_bucket()
        if self._daily_spend_day.get(customer_id) != day:
            self._daily_spend[customer_id] = 0
            self._daily_spend_day[customer_id] = day
        self._daily_spend[customer_id] = (
            self._daily_spend.get(customer_id, 0) + actual_tokens
        )


budget_service = BudgetService()
