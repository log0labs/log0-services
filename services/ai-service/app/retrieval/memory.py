"""Long-term memory: what earlier investigations concluded, recalled when a similar incident comes up.

* **Write**: when an investigation ends with a root cause, its conclusion is stored with an embedding
  (``remember``). One memory per investigation; a re-run replaces it but keeps the engineer's feedback.
* **Feedback**: an engineer marks the result helpful, partly helpful, or wrong (``record_feedback``).
* **Recall**: a new investigation looks up the nearest memories (``recall``). Wrong ones are never recalled;
  helpful ones rank higher, partly helpful ones slightly lower. A recalled memory is a lead, never proof:
  the evidence text says so, and only evidence gathered in this run can ground a root cause.

Everything is per tenant. The incident being investigated is excluded (it would only recall itself).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.retrieval.embeddings import Embedder
from app.security.redaction import redact_text
from app.store import memory as store

RECALL_LIMIT = 3
FETCH_LIMIT = 8  # read more than shown, so ranking by feedback has something to reorder
MAX_DISTANCE = 0.75  # farther than this is unrelated
MAX_TEXT = 1500

# Added to the distance when ranking: lower is better.
FEEDBACK_ADJUSTMENT = {"helpful": -0.10, "partly": 0.05, None: 0.0}


@dataclass(frozen=True)
class NewMemory:
    tenant_id: UUID
    investigation_id: UUID
    incident_id: UUID
    service_name: str | None
    root_cause: str
    recommendation: str | None
    confidence_band: str | None


@dataclass(frozen=True)
class RecalledInvestigation:
    investigation_id: UUID
    incident_id: UUID
    service_name: str | None
    root_cause: str
    recommendation: str | None
    confidence_band: str | None
    feedback_verdict: str | None
    feedback_note: str | None
    created_at: datetime
    distance: float


class Memory(Protocol):
    """What the graph and the API need. ``PgMemory`` in production, a fake in tests."""

    async def remember(self, memory: NewMemory) -> None: ...

    async def recall(
        self, *, tenant_id: UUID, query: str, exclude_incident_id: UUID | None
    ) -> list[RecalledInvestigation]: ...

    async def record_feedback(
        self, *, tenant_id: UUID, investigation_id: UUID, verdict: str, note: str | None, user_id: UUID
    ) -> bool: ...


def hide_secrets(memory: NewMemory) -> NewMemory:
    """The conclusion is model-written text about redacted evidence, but it is stored for months and fed to future
    prompts, so it gets one more pass."""
    return NewMemory(
        tenant_id=memory.tenant_id,
        investigation_id=memory.investigation_id,
        incident_id=memory.incident_id,
        service_name=memory.service_name,
        root_cause=redact_text(memory.root_cause),
        recommendation=redact_text(memory.recommendation) if memory.recommendation else None,
        confidence_band=memory.confidence_band,
    )


def memory_text(memory: NewMemory) -> str:
    """What is embedded: the service, the root cause and the fix, the things a later incident would resemble."""
    parts = [memory.service_name or "", memory.root_cause]
    if memory.recommendation:
        parts.append(memory.recommendation)
    return " ".join(" ".join(parts).split())[:MAX_TEXT]


def rank_memories(rows: list[store.MemoryRow], *, limit: int = RECALL_LIMIT) -> list[RecalledInvestigation]:
    """Drop unrelated and wrong memories, order by distance adjusted for feedback, keep ``limit``."""
    usable = [
        r
        for r in rows
        if r.feedback_verdict != "wrong" and r.distance is not None and r.distance <= MAX_DISTANCE
    ]
    usable.sort(key=lambda r: ((r.distance or 0.0) + FEEDBACK_ADJUSTMENT.get(r.feedback_verdict, 0.0), -r.created_at.timestamp()))
    return [
        RecalledInvestigation(
            investigation_id=r.investigation_id,
            incident_id=r.incident_id,
            service_name=r.service_name,
            root_cause=r.root_cause,
            recommendation=r.recommendation,
            confidence_band=r.confidence_band,
            feedback_verdict=r.feedback_verdict,
            feedback_note=r.feedback_note,
            created_at=r.created_at,
            distance=r.distance or 0.0,
        )
        for r in usable[:limit]
    ]


class PgMemory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self._session_factory = session_factory
        self._embedder = embedder

    async def remember(self, memory: NewMemory) -> None:
        memory = hide_secrets(memory)
        vector = await self._embedder.embed_query(memory_text(memory))
        async with self._session_factory() as session:
            await store.upsert(
                session,
                tenant_id=memory.tenant_id,
                investigation_id=memory.investigation_id,
                incident_id=memory.incident_id,
                service_name=memory.service_name,
                root_cause=memory.root_cause,
                recommendation=memory.recommendation,
                confidence_band=memory.confidence_band,
                embedding_model=self._embedder.model_id,
                vector=vector,
            )

    async def recall(
        self, *, tenant_id: UUID, query: str, exclude_incident_id: UUID | None
    ) -> list[RecalledInvestigation]:
        vector = await self._embedder.embed_query(query)
        async with self._session_factory() as session:
            rows = await store.search(
                session,
                tenant_id=tenant_id,
                query_vector=vector,
                embedding_model=self._embedder.model_id,
                exclude_incident_id=exclude_incident_id,
                limit=FETCH_LIMIT,
            )
        return rank_memories(rows)

    async def record_feedback(
        self, *, tenant_id: UUID, investigation_id: UUID, verdict: str, note: str | None, user_id: UUID
    ) -> bool:
        async with self._session_factory() as session:
            return await store.set_feedback(
                session, tenant_id=tenant_id, investigation_id=investigation_id, verdict=verdict, note=note, user_id=user_id
            )
