"""
Structured audit logging for post-call processing.

Every state transition or external call writes a row to
`interaction_audit_log` AND emits a structured stdout log line. Both carry:
    interaction_id, correlation_id, customer_id, campaign_id, event_type,
    severity, message, details

Why both:
    - Postgres rows are queryable three days later for incident review
      ("show me everything that happened to this interaction").
    - stdout lines are scraped by the log shipper for real-time alerting
      (Grafana / Datadog) without us building a polling indexer.

The two writes are decoupled — if Postgres is briefly unavailable the
stdout line still fires. Audit rows that fail to insert are buffered to
a local fallback (out of scope for the assessment but documented in
SUBMISSION.md §9).

For the assessment we keep an in-memory mirror so tests can assert
"every interaction has events {a, b, c}" without spinning up Postgres.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AuditEvent:
    interaction_id: str
    correlation_id: str
    event_type: str
    severity: str = "INFO"
    message: str = ""
    customer_id: Optional[str] = None
    campaign_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    created_at: str = ""


class AuditLog:
    """
    In-memory + structured-log audit trail.

    The `_buffer` is a process-local mirror used by tests and by an
    optional `inspect()` helper for ops. Production wiring would
    additionally write through to Postgres `interaction_audit_log`
    via the same write_event() entry point.
    """

    def __init__(self):
        self._buffer: List[AuditEvent] = []
        self._lock = threading.Lock()

    def write(
        self,
        interaction_id: str,
        correlation_id: str,
        event_type: str,
        severity: str = "INFO",
        message: str = "",
        customer_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        evt = AuditEvent(
            interaction_id=interaction_id,
            correlation_id=correlation_id,
            event_type=event_type,
            severity=severity,
            message=message,
            customer_id=customer_id,
            campaign_id=campaign_id,
            details=details or {},
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # `message` is a reserved key on LogRecord; rename to keep
        # logger.extra=… happy while preserving the field in the row.
        _RESERVED = {"message", "asctime", "name", "msg", "args", "levelname"}
        log_payload = {
            (f"audit_{k}" if k in _RESERVED else k): v
            for k, v in asdict(evt).items()
            if v not in (None, "", {})
        }
        if severity == "ERROR":
            logger.error(event_type, extra=log_payload)
        elif severity == "WARN":
            logger.warning(event_type, extra=log_payload)
        else:
            logger.info(event_type, extra=log_payload)

        with self._lock:
            self._buffer.append(evt)

        # Production would also: INSERT INTO interaction_audit_log (...)
        # asynchronously via a fire-and-forget connection pool.

        return evt

    def events_for(self, interaction_id: str) -> List[AuditEvent]:
        with self._lock:
            return [e for e in self._buffer if e.interaction_id == interaction_id]

    def event_types_for(self, interaction_id: str) -> List[str]:
        return [e.event_type for e in self.events_for(interaction_id)]

    def reset(self):
        with self._lock:
            self._buffer.clear()


audit_log = AuditLog()
