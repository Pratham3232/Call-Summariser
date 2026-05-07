import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.post_call_processor import PostCallContext


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Fake Redis -------------------------------------------------------------
# Just enough surface area for the rate limiter, retry queue, and metrics
# tracker to work in tests without a real Redis. Implements EVAL so the
# token-bucket Lua script runs through the same code path as production.


class FakeRedis:
    def __init__(self):
        self._kv: Dict[str, Any] = {}
        self._hash: Dict[str, Dict[str, str]] = {}
        self._ttl: Dict[str, float] = {}
        self._scripts: Dict[str, str] = {}

    async def get(self, k):
        if k in self._ttl and time.time() > self._ttl[k]:
            self._kv.pop(k, None); self._ttl.pop(k, None)
            return None
        v = self._kv.get(k)
        return v

    async def set(self, k, v, ex=None):
        self._kv[k] = str(v)
        if ex:
            self._ttl[k] = time.time() + ex

    async def incr(self, k):
        cur = int(self._kv.get(k, 0))
        cur += 1
        self._kv[k] = str(cur)
        return cur

    async def decr(self, k):
        cur = int(self._kv.get(k, 0))
        cur -= 1
        self._kv[k] = str(cur)
        return cur

    async def expire(self, k, sec):
        self._ttl[k] = time.time() + sec

    async def hset(self, k, field=None, value=None, mapping=None):
        h = self._hash.setdefault(k, {})
        if mapping:
            for f, v in mapping.items():
                h[f] = str(v)
        elif field is not None:
            h[field] = str(value)

    async def hget(self, k, field):
        return self._hash.get(k, {}).get(field)

    async def hmget(self, k, *fields):
        h = self._hash.get(k, {})
        return [h.get(f) for f in fields]

    async def rpush(self, k, v):
        lst = self._kv.setdefault(k, [])
        if not isinstance(lst, list):
            lst = []
            self._kv[k] = lst
        lst.append(v)

    async def lpop(self, k):
        lst = self._kv.get(k, [])
        if not isinstance(lst, list) or not lst:
            return None
        return lst.pop(0)

    async def llen(self, k):
        lst = self._kv.get(k, [])
        return len(lst) if isinstance(lst, list) else 0

    async def script_load(self, script):
        sha = f"sha-{abs(hash(script))}"
        self._scripts[sha] = script
        return sha

    async def evalsha(self, sha, numkeys, *args):
        return await self._eval_inner(self._scripts[sha], numkeys, *args)

    async def eval(self, script, numkeys, *args):
        return await self._eval_inner(script, numkeys, *args)

    async def _eval_inner(self, script, numkeys, *args):
        # Implements the token-bucket Lua script. We don't actually parse Lua;
        # we hard-code the semantics to match the script in rate_limiter.py.
        key = args[0]
        capacity = float(args[1])
        rate = float(args[2])
        cost = float(args[3])
        now_ms = float(args[4])

        h = self._hash.setdefault(key, {})
        tokens = float(h.get("tokens", capacity))
        updated_ms = float(h.get("updated_ms", now_ms))

        elapsed = max(0, now_ms - updated_ms) / 1000.0
        tokens = min(capacity, tokens + elapsed * rate)

        if tokens >= cost:
            tokens -= cost
            h["tokens"] = str(tokens)
            h["updated_ms"] = str(now_ms)
            return [1, str(tokens)]
        else:
            h["tokens"] = str(tokens)
            h["updated_ms"] = str(now_ms)
            needed = cost - tokens
            retry_after_ms = int((needed / rate) * 1000) if rate > 0 else 60_000
            return [0, str(retry_after_ms)]

    async def script_flush(self):
        self._scripts.clear()

    def reset(self):
        self._kv.clear()
        self._hash.clear()
        self._ttl.clear()


# Singleton fake redis injected into modules that import redis_client.
_fake = FakeRedis()


@pytest.fixture(autouse=True)
def patch_redis(monkeypatch):
    """Replace redis_client with the FakeRedis for every test."""
    import src.utils.redis_client as rc
    import src.services.rate_limiter as rl

    _fake.reset()
    monkeypatch.setattr(rc, "redis_client", _fake)
    monkeypatch.setattr(rl, "redis_client", _fake)
    yield


@pytest.fixture(autouse=True)
def reset_state():
    """Clean global stores between tests."""
    from src.services.outbox import outbox
    from src.services.audit_log import audit_log
    from src.services.dlq import dlq_store
    from src.services.usage_tracker import usage_tracker
    from src.services.analysis_worker import analysis_store
    from src.services.budget import budget_service, customer_registry, CustomerConfig
    from src.services.rate_limiter import rate_limiter

    outbox.reset()
    audit_log.reset()
    dlq_store.reset()
    usage_tracker.reset()
    analysis_store.reset()
    budget_service._daily_spend.clear()
    budget_service._daily_spend_day.clear()
    rate_limiter._sha = None  # force reload against fresh fake redis

    # Pre-register the seed customers used in the fixtures.
    customer_registry._cache.clear()
    customer_registry.register(
        CustomerConfig(
            customer_id="d0000000-0000-0000-0000-000000000001",
            name="Cashify",
            tokens_per_minute_reservation=20000,
            priority=200,
        )
    )
    customer_registry.register(
        CustomerConfig(
            customer_id="d0000000-0000-0000-0000-000000000002",
            name="Acme CRM",
            tokens_per_minute_reservation=10000,
            priority=100,
        )
    )
    yield


# ── Test data fixtures -----------------------------------------------------


@pytest.fixture
def sample_transcripts():
    with open(FIXTURES_DIR / "sample_transcripts.json") as f:
        return json.load(f)["transcripts"]


@pytest.fixture
def make_post_call_context(sample_transcripts):
    def _factory(transcript_key: str = "rebook_confirmed", **overrides):
        transcript_data = sample_transcripts[transcript_key]
        transcript = transcript_data["transcript"]
        transcript_text = "\n".join(
            f"{turn['role']}: {turn['content']}" for turn in transcript
        )
        defaults = {
            "interaction_id": "test-interaction-001",
            "session_id": "test-session-001",
            "lead_id": "test-lead-001",
            "campaign_id": "test-campaign-001",
            "customer_id": "test-customer-001",
            "agent_id": "test-agent-001",
            "call_sid": "test-call-sid-001",
            "transcript_text": transcript_text,
            "conversation_data": {"transcript": transcript},
            "additional_data": {},
            "ended_at": datetime.now(timezone.utc),
            "exotel_account_id": "test-exotel-account",
        }
        defaults.update(overrides)
        return PostCallContext(**defaults)
    return _factory


def make_payload(
    interaction_id: str,
    customer_id: str,
    transcript: List[Dict[str, str]],
    *,
    campaign_id: str = "test-campaign-001",
    lead_id: str = "test-lead-001",
    agent_id: str = "test-agent-001",
    session_id: str = "test-session-001",
    call_sid: Optional[str] = "test-call-sid",
) -> Dict[str, Any]:
    transcript_text = "\n".join(
        f"{t.get('role')}: {t.get('content')}" for t in transcript
    )
    return {
        "interaction_id": interaction_id,
        "session_id": session_id,
        "lead_id": lead_id,
        "campaign_id": campaign_id,
        "customer_id": customer_id,
        "agent_id": agent_id,
        "call_sid": call_sid,
        "transcript_text": transcript_text,
        "conversation_data": {"transcript": transcript},
        "additional_data": {},
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "exotel_account_id": "exotel-test",
    }


@pytest.fixture
def payload_factory(sample_transcripts):
    def _make(
        transcript_key: str,
        interaction_id: str,
        customer_id: str = "d0000000-0000-0000-0000-000000000001",
    ):
        return make_payload(
            interaction_id=interaction_id,
            customer_id=customer_id,
            transcript=sample_transcripts[transcript_key]["transcript"],
        )
    return _make


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.decr = AsyncMock(return_value=0)
    redis.expire = AsyncMock()
    redis.rpush = AsyncMock()
    redis.lpop = AsyncMock(return_value=None)
    redis.llen = AsyncMock(return_value=0)
    redis.hset = AsyncMock()
    redis.hget = AsyncMock(return_value=None)
    redis.pipeline = MagicMock()
    return redis
