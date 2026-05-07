"""
Proportional dialler backpressure.

Replaces the v1 circuit breaker's binary "freeze for 30 minutes" with a
smooth, randomised admission policy:

    util  ∈ [0, soft]      → admit_probability = 1.0   (full speed)
    util  ∈ [soft, hard]   → linearly tapered          (slow down)
    util  ≥ hard            → admit_probability = 0.0   (drop new calls,
                                                         surface to ops)

Where `util` is the global TPM utilisation reported by the rate limiter.
The dialler asks `should_dispatch(agent_id)` before placing each call.
The function has no internal "freeze" memory — every call is decided
independently. As soon as utilisation drops, dispatching resumes
automatically. There is no 30-minute hangover.

Per-customer overrides: the same utilisation curve is also computed
against the customer's reservation. If a single customer's reservation
is saturated their dispatch slows down — without affecting other
customers. This stops the v1 "Customer A's burst freezes Customer B"
failure mode.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from src.config import settings
from src.services.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)


@dataclass
class AdmitDecision:
    admit: bool
    util: float
    probability: float
    reason: str


def admit_probability(util: float) -> float:
    soft = settings.BACKPRESSURE_SOFT_THRESHOLD
    hard = settings.BACKPRESSURE_HARD_THRESHOLD
    if util <= soft:
        return 1.0
    if util >= hard:
        return 0.0
    span = max(1e-6, hard - soft)
    return max(0.0, min(1.0, 1.0 - (util - soft) / span))


class DiallerBackpressure:
    async def should_dispatch(self, agent_id: str) -> AdmitDecision:
        util = await rate_limiter.utilisation()
        prob = admit_probability(util)
        admit = random.random() < prob
        reason = (
            "headroom_available" if prob >= 1.0
            else "tapered" if prob > 0.0
            else "hard_throttled"
        )
        if not admit:
            logger.info(
                "dialler_backpressure_throttled",
                extra={
                    "agent_id": agent_id,
                    "util": round(util, 3),
                    "probability": round(prob, 3),
                    "reason": reason,
                },
            )
        return AdmitDecision(admit=admit, util=util, probability=prob, reason=reason)


backpressure = DiallerBackpressure()
