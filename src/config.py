import os


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/voicebot"
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
    CELERY_RESULT_BACKEND: str = os.getenv(
        "CELERY_RESULT_BACKEND", "redis://localhost:6379/2"
    )

    # ── LLM ───────────────────────────────────────────────────────────────────
    # One provider, one model, one key. Everyone shares it.
    # These limits come straight from the provider's dashboard — they are HARD
    # limits that result in 429 errors when exceeded, not soft suggestions.
    #
    # At 100K calls/campaign: if even 10% hit the LLM concurrently that's
    # 10,000 requests fighting for 500 slots/min. You do the math.
    #
    # Worth noting: LLM_TOKENS_PER_MINUTE and LLM_REQUESTS_PER_MINUTE are
    # defined here but grep the codebase — nothing actually reads them before
    # firing a request. They exist as documentation, not enforcement.
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "sk-mock-key-for-assessment")
    LLM_TOKENS_PER_MINUTE: int = int(os.getenv("LLM_TOKENS_PER_MINUTE", "90000"))
    LLM_REQUESTS_PER_MINUTE: int = int(os.getenv("LLM_REQUESTS_PER_MINUTE", "500"))

    # Average tokens consumed per post-call analysis (measured from prod logs).
    # Useful if you're trying to estimate how many calls can be processed per
    # minute before hitting LLM_TOKENS_PER_MINUTE.
    LLM_AVG_TOKENS_PER_CALL: int = int(os.getenv("LLM_AVG_TOKENS_PER_CALL", "1500"))

    # ── Recording ─────────────────────────────────────────────────────────────
    # Why 45 seconds? Someone measured the average Exotel delivery time once,
    # added a buffer, and hardcoded it. That was on a quiet Friday afternoon.
    # Under load the delivery window is 10s–120s with no guarantee.
    RECORDING_WAIT_SECONDS: int = 45
    S3_BUCKET: str = os.getenv("S3_BUCKET", "voicebot-recordings")

    # ── Circuit breaker ───────────────────────────────────────────────────────
    # When LLM usage hits 90% of capacity, the circuit breaker trips and the
    # dialler freezes for 30 minutes. This was meant to prevent 429s.
    # In practice it just means the dialler stops making calls while the LLM
    # queue drains — business impact: zero new calls for half an hour.
    #
    # 1800 seconds = 30 minutes. The sales team noticed before the engineers did.
    CIRCUIT_BREAKER_CAPACITY_THRESHOLD: float = 0.90
    CIRCUIT_BREAKER_FREEZE_SECONDS: int = 1800

    # ── Post-call processing ──────────────────────────────────────────────────
    # Legacy single-queue retained for backward compatibility with the v1 task
    # name. v2 routes to lane-aware queues below.
    POSTCALL_CELERY_QUEUE: str = "postcall_processing"
    POSTCALL_MAX_RETRIES: int = 3
    POSTCALL_RETRY_DELAY: int = 60

    # ── v2: Lane-aware queues ────────────────────────────────────────────────
    # Hot lane is for time-sensitive outcomes (bookings, escalations).
    # Cold lane is for low-value or deferrable outcomes (not interested,
    # already done) which can be processed when the global TPM has headroom.
    POSTCALL_HOT_QUEUE: str = "postcall_hot"
    POSTCALL_COLD_QUEUE: str = "postcall_cold"

    # ── v2: Rate limiter ─────────────────────────────────────────────────────
    # Sliding-window token bucket fill rates. Bucket capacity = rate * 1.0
    # (one minute of headroom — same units as the provider's published limit).
    LLM_BUDGET_RESERVED_FRACTION: float = float(
        os.getenv("LLM_BUDGET_RESERVED_FRACTION", "0.6")
    )
    # Above the per-customer reservation, customers compete for this fraction
    # of the global pool. Anything left over (1 - reserved - shared) is held
    # back as a safety margin so a sudden spike from one customer cannot
    # consume 100% of the provider limit.
    LLM_BUDGET_SHARED_FRACTION: float = float(
        os.getenv("LLM_BUDGET_SHARED_FRACTION", "0.3")
    )

    # ── v2: Recording poller ─────────────────────────────────────────────────
    RECORDING_POLL_INITIAL_DELAY_SECONDS: int = int(
        os.getenv("RECORDING_POLL_INITIAL_DELAY_SECONDS", "5")
    )
    RECORDING_POLL_MAX_ATTEMPTS: int = int(
        os.getenv("RECORDING_POLL_MAX_ATTEMPTS", "8")
    )
    RECORDING_POLL_MAX_DELAY_SECONDS: int = int(
        os.getenv("RECORDING_POLL_MAX_DELAY_SECONDS", "300")
    )

    # ── v2: Outbox / job runner ─────────────────────────────────────────────
    JOB_VISIBILITY_TIMEOUT_SECONDS: int = int(
        os.getenv("JOB_VISIBILITY_TIMEOUT_SECONDS", "120")
    )
    JOB_MAX_ATTEMPTS: int = int(os.getenv("JOB_MAX_ATTEMPTS", "8"))

    # ── v2: Backpressure ─────────────────────────────────────────────────────
    # Proportional backpressure replaces the binary 1800s freeze.
    # admit_probability(util) = clamp(1 - max(0, util - SOFT) / (HARD - SOFT), 0, 1)
    BACKPRESSURE_SOFT_THRESHOLD: float = float(
        os.getenv("BACKPRESSURE_SOFT_THRESHOLD", "0.70")
    )
    BACKPRESSURE_HARD_THRESHOLD: float = float(
        os.getenv("BACKPRESSURE_HARD_THRESHOLD", "0.95")
    )

    # ── v2: Alerts ──────────────────────────────────────────────────────────
    ALERT_TPM_UTILISATION: float = float(os.getenv("ALERT_TPM_UTILISATION", "0.85"))
    ALERT_QUEUE_DEPTH_HOT: int = int(os.getenv("ALERT_QUEUE_DEPTH_HOT", "1000"))
    ALERT_RECORDING_DEAD_LETTERED: int = int(
        os.getenv("ALERT_RECORDING_DEAD_LETTERED", "10")
    )


settings = Settings()
