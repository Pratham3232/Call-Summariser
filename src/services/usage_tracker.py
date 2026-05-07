"""
Token usage tracker.

Two-tier:
    - Postgres `token_usage` ledger (source of truth, billing).
    - Redis sliding-window counters per customer (real-time visibility,
      dashboards, "are we trending toward 90% TPM?" alerts).

The ledger is append-only — every LLM response writes one row. The Redis
counters are a 60-second window of summed tokens; they re-derive themselves
naturally from refresh.

For the assessment we keep a process-local mirror keyed by customer_id
so tests can assert per-customer spend without a Postgres connection.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger(__name__)


@dataclass
class UsageEntry:
    interaction_id: str
    customer_id: str
    campaign_id: str
    tokens: int
    provider: str
    model: str
    created_at: float = field(default_factory=time.time)


class UsageTracker:
    def __init__(self):
        self._entries: List[UsageEntry] = []
        self._lock = threading.Lock()

    async def record(
        self,
        *,
        interaction_id: str,
        customer_id: str,
        campaign_id: str,
        tokens: int,
        provider: str,
        model: str,
    ):
        entry = UsageEntry(
            interaction_id=interaction_id,
            customer_id=customer_id,
            campaign_id=campaign_id,
            tokens=tokens,
            provider=provider,
            model=model,
        )
        with self._lock:
            self._entries.append(entry)

        # In production also: INSERT INTO token_usage(...) and INCRBY
        # cust:tpm:{customer_id} on a sliding-window key.
        logger.info(
            "token_usage_recorded",
            extra={
                "interaction_id": interaction_id,
                "customer_id": customer_id,
                "campaign_id": campaign_id,
                "tokens": tokens,
                "provider": provider,
                "model": model,
            },
        )

    def total_for_customer(self, customer_id: str) -> int:
        with self._lock:
            return sum(e.tokens for e in self._entries if e.customer_id == customer_id)

    def per_customer_totals(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        with self._lock:
            for e in self._entries:
                out[e.customer_id] = out.get(e.customer_id, 0) + e.tokens
        return out

    def reset(self):
        with self._lock:
            self._entries.clear()


usage_tracker = UsageTracker()
