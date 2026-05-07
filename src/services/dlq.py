"""
Dead letter queue store.

Anything that exhausts retries lands here with the FULL original payload so an
operator can replay it with a single SQL UPDATE (or a CLI command). This is
the durability backstop — once a job reaches max_attempts we have NEVER
silently dropped it.

The on-call alert "DLQ depth > N" fires off this table.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DLQEntry:
    id: str
    original_job_id: Optional[str]
    interaction_id: Optional[str]
    customer_id: Optional[str]
    job_type: str
    payload: Dict[str, Any]
    last_error: Optional[str]
    attempts: int
    correlation_id: Optional[str]
    created_at: float = field(default_factory=time.time)
    replayed_at: Optional[float] = None


class DLQStore:
    def __init__(self):
        self._entries: Dict[str, DLQEntry] = {}
        self._lock = threading.Lock()

    def add(
        self,
        *,
        original_job_id: Optional[str],
        interaction_id: Optional[str],
        customer_id: Optional[str],
        job_type: str,
        payload: Dict[str, Any],
        last_error: Optional[str],
        attempts: int,
        correlation_id: Optional[str],
    ) -> DLQEntry:
        entry = DLQEntry(
            id=str(uuid.uuid4()),
            original_job_id=original_job_id,
            interaction_id=interaction_id,
            customer_id=customer_id,
            job_type=job_type,
            payload=payload,
            last_error=last_error,
            attempts=attempts,
            correlation_id=correlation_id,
        )
        with self._lock:
            self._entries[entry.id] = entry
        return entry

    def all(self, unreplayed_only: bool = True) -> List[DLQEntry]:
        with self._lock:
            return [
                e for e in self._entries.values()
                if not unreplayed_only or e.replayed_at is None
            ]

    def find_by_interaction(self, interaction_id: str) -> List[DLQEntry]:
        return [
            e for e in self._entries.values()
            if e.interaction_id == interaction_id
        ]

    def mark_replayed(self, entry_id: str):
        with self._lock:
            e = self._entries.get(entry_id)
            if e:
                e.replayed_at = time.time()

    def reset(self):
        with self._lock:
            self._entries.clear()


dlq_store = DLQStore()
