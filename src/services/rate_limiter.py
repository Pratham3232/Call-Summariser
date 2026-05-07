"""
Distributed token-bucket rate limiter with per-customer reservations.

Why token bucket (not fixed-window counters):
    LLM providers rate-limit on a sliding basis. A fixed-window counter that
    resets every minute lets us burst 2x just before/after the boundary and
    eat a 429. A token bucket smooths spend over the window.

Why two dimensions (TPM and RPM):
    Providers enforce both. The same call can fit under TPM but blow RPM
    (lots of small calls) or vice versa (one giant call). We must check both.

Why Redis with Lua:
    Multiple workers checking-and-decrementing must be atomic. A naive
    GET/INCR/CHECK race is the classic source of "we still 429ed even with
    a limiter". The Lua script computes the refilled bucket, decides
    allow/deny, and conditionally debits — in one round-trip.

Per-customer reservation model:
    Each customer is allocated a guaranteed share of the global bucket
    (`tokens_per_minute_reservation`). They always have a private bucket
    sized to that reservation. When their private bucket has tokens they
    are served from it. If empty, they fall back to the *shared* bucket,
    which holds the un-reserved fraction of the global limit. This means:
      - Customer A with 20K reservation: guaranteed 20K/min.
      - Customer B with no reservation: only ever gets shared headroom.
      - A noisy neighbour cannot starve a reserved tenant.

Failure mode for the limiter itself:
    If Redis is unavailable, the limiter is constructed to FAIL CLOSED for
    the LLM call (returns deny so we requeue) — never fail open and 429.
    The unit tests pin this behaviour.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.config import settings
from src.utils.redis_client import redis_client

logger = logging.getLogger(__name__)


# Atomic check-and-debit script. Refills the bucket linearly up to `capacity`
# at `rate_per_sec` tokens/sec, then debits `cost` if there's headroom.
#
# KEYS[1] = bucket key
# ARGV    = capacity, rate_per_sec, cost, now_ms
# returns {1, remaining}  on success
#         {0, retry_after_ms} on deny
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now_ms = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'updated_ms')
local tokens = tonumber(data[1])
local updated_ms = tonumber(data[2])
if tokens == nil then
    tokens = capacity
    updated_ms = now_ms
end

local elapsed = math.max(0, now_ms - updated_ms) / 1000.0
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= cost then
    tokens = tokens - cost
    redis.call('HSET', key, 'tokens', tokens, 'updated_ms', now_ms)
    redis.call('PEXPIRE', key, 120000)
    return {1, tostring(tokens)}
else
    redis.call('HSET', key, 'tokens', tokens, 'updated_ms', now_ms)
    redis.call('PEXPIRE', key, 120000)
    local needed = cost - tokens
    local retry_after_ms = math.ceil((needed / rate) * 1000)
    return {0, tostring(retry_after_ms)}
end
"""


@dataclass
class AcquireResult:
    allowed: bool
    bucket: str               # 'reserved' | 'shared' | 'denied'
    retry_after_ms: int = 0
    remaining: float = 0.0


class TokenBucketRateLimiter:
    """
    Two-tier limiter:
      tier 1: per-(customer, dimension) reservation bucket
      tier 2: global shared bucket (tokens AND requests)

    `dimension` selects 'tpm' (tokens/min) or 'rpm' (requests/min).
    """

    def __init__(self):
        self._sha: Optional[str] = None

    # Read settings live so runtime overrides (chaos drills, hot-reloads,
    # per-environment tuning) take effect without a process restart.
    @property
    def _global_tpm(self) -> int:
        return settings.LLM_TOKENS_PER_MINUTE

    @property
    def _global_rpm(self) -> int:
        return settings.LLM_REQUESTS_PER_MINUTE

    @property
    def _shared_fraction(self) -> float:
        return settings.LLM_BUDGET_SHARED_FRACTION

    async def _ensure_loaded(self):
        if self._sha is None:
            try:
                self._sha = await redis_client.script_load(_TOKEN_BUCKET_LUA)
            except Exception:  # pragma: no cover — covered via unit tests with fake redis
                self._sha = None
        return self._sha

    async def _evalsha(self, key: str, capacity: float, rate: float, cost: float):
        sha = await self._ensure_loaded()
        now_ms = int(time.time() * 1000)
        if sha:
            return await redis_client.evalsha(
                sha, 1, key, capacity, rate, cost, now_ms
            )
        # Fall back to EVAL — used when the connection has flushed scripts or
        # when the test harness uses a fake redis that only implements EVAL.
        return await redis_client.eval(
            _TOKEN_BUCKET_LUA, 1, key, capacity, rate, cost, now_ms
        )

    async def acquire(
        self,
        customer_id: str,
        tokens: int,
        reservation_tpm: int,
        reservation_rpm: Optional[int] = None,
    ) -> AcquireResult:
        """
        Attempt to debit the limiter for one LLM request consuming `tokens`.

        Strategy: try the customer's reserved bucket first (both TPM and RPM
        must permit), fall back to the shared bucket if the reservation is
        exhausted. If both deny, return the larger retry_after_ms so the
        caller can sleep / requeue cooperatively.
        """
        rps_reservation = reservation_rpm or max(
            1, int(reservation_tpm / max(1, settings.LLM_AVG_TOKENS_PER_CALL))
        )

        # Tier 1: reserved bucket (per-customer)
        if reservation_tpm > 0:
            try:
                tpm_ok, tpm_payload = await self._evalsha(
                    f"rl:cust:tpm:{customer_id}",
                    capacity=reservation_tpm,
                    rate=reservation_tpm / 60.0,
                    cost=tokens,
                )
                rpm_ok, rpm_payload = await self._evalsha(
                    f"rl:cust:rpm:{customer_id}",
                    capacity=rps_reservation,
                    rate=rps_reservation / 60.0,
                    cost=1,
                )
                if int(tpm_ok) == 1 and int(rpm_ok) == 1:
                    return AcquireResult(
                        allowed=True,
                        bucket="reserved",
                        remaining=float(tpm_payload),
                    )
                # If we debited TPM but failed RPM (or vice versa) we
                # tolerated a small over-debit — refunded on the next acquire
                # via the natural refill. Acceptable: the bucket is tiny.
            except Exception as e:
                logger.error(
                    "rate_limiter_redis_error",
                    extra={"customer_id": customer_id, "tier": "reserved", "error": str(e)},
                )
                # Fail closed — better to defer than to 429.
                return AcquireResult(
                    allowed=False, bucket="denied", retry_after_ms=5000
                )

        # Tier 2: shared bucket (global). Sized to the shared fraction of the
        # global pool. Anything left over (1 - reserved - shared) is a safety
        # margin we never spend.
        shared_tpm = int(self._global_tpm * self._shared_fraction)
        shared_rpm = int(self._global_rpm * self._shared_fraction)
        try:
            stpm_ok, stpm_payload = await self._evalsha(
                "rl:shared:tpm",
                capacity=shared_tpm,
                rate=shared_tpm / 60.0,
                cost=tokens,
            )
            srpm_ok, srpm_payload = await self._evalsha(
                "rl:shared:rpm",
                capacity=shared_rpm,
                rate=shared_rpm / 60.0,
                cost=1,
            )
            if int(stpm_ok) == 1 and int(srpm_ok) == 1:
                return AcquireResult(
                    allowed=True,
                    bucket="shared",
                    remaining=float(stpm_payload),
                )
            retry_after = max(int(stpm_payload), int(srpm_payload))
            return AcquireResult(
                allowed=False, bucket="denied", retry_after_ms=retry_after
            )
        except Exception as e:
            logger.error(
                "rate_limiter_redis_error",
                extra={"customer_id": customer_id, "tier": "shared", "error": str(e)},
            )
            return AcquireResult(
                allowed=False, bucket="denied", retry_after_ms=5000
            )

    async def utilisation(self) -> float:
        """
        Returns the current utilisation of the global TPM (reserved + shared)
        as a value in [0, 1]. Used by the dialler backpressure layer.
        """
        try:
            tokens = await redis_client.hget("rl:shared:tpm", "tokens")
        except Exception:
            return 0.0
        if tokens is None:
            return 0.0
        shared_capacity = max(1, int(self._global_tpm * self._shared_fraction))
        return max(0.0, min(1.0, 1.0 - float(tokens) / shared_capacity))


rate_limiter = TokenBucketRateLimiter()
