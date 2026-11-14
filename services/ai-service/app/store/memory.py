"""Persistence for ai.investigation_memory: what past investigations concluded, and what engineers thought of it.

Every function takes ``tenant_id`` and filters on it. Vectors travel as text through ``CAST(:v AS vector)``.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.embeddings import to_pgvector

VERDICTS = ("helpful", "partly", "wrong")


@dataclass(frozen=True)
class MemoryRow:
    investigation_id: UUID
    incident_id: UUID
    service_name: str | None
    root_cause: str
    recommendation: str | None
    confidence_band: str | None
    feedback_verdict: str | None
    feedback_note: str | None
    created_at: datetime
    distance: float | None = None  # set by ``search``: cosine distance to the query


_COLUMNS = (
    "investigation_id, incident_id, service_name, root_cause, recommendation, confidence_band, "
    "feedback_verdict, feedback_note, created_at"
)


def _row(row) -> MemoryRow:
    m = row._mapping
    return MemoryRow(**{f: m[f] for f in MemoryRow.__dataclass_fields__ if f in m})


async def upsert(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    investigation_id: UUID,
    incident_id: UUID,
    service_name: str | None,
    root_cause: str,
    recommendation: str | None,
    confidence_band: str | None,
    embedding_model: str,
    vector: list[float],
) -> None:
    """Write the memory of an investigation; a second write (a re-run) replaces the text but keeps the feedback."""
    await session.execute(
        text(
            "INSERT INTO ai.investigation_memory (tenant_id, investigation_id, incident_id, service_name, root_cause, "
            "  recommendation, confidence_band, embedding_model, embedding) "
            "VALUES (:t, :inv, :inc, :svc, :rc, :rec, :band, :model, CAST(:v AS vector)) "
            "ON CONFLICT (investigation_id) DO UPDATE SET service_name = EXCLUDED.service_name, "
            "  root_cause = EXCLUDED.root_cause, recommendation = EXCLUDED.recommendation, "
            "  confidence_band = EXCLUDED.confidence_band, embedding_model = EXCLUDED.embedding_model, "
            "  embedding = EXCLUDED.embedding, updated_at = now() "
            "WHERE ai.investigation_memory.tenant_id = EXCLUDED.tenant_id"
        ),
        {
            "t": tenant_id,
            "inv": investigation_id,
            "inc": incident_id,
            "svc": service_name,
            "rc": root_cause,
            "rec": recommendation,
            "band": confidence_band,
            "model": embedding_model,
            "v": to_pgvector(vector),
        },
    )
    await session.commit()


async def get(session: AsyncSession, *, tenant_id: UUID, investigation_id: UUID) -> MemoryRow | None:
    result = await session.execute(
        text(f"SELECT {_COLUMNS} FROM ai.investigation_memory WHERE tenant_id = :t AND investigation_id = :inv"),
        {"t": tenant_id, "inv": investigation_id},
    )
    row = result.first()
    return _row(row) if row else None


async def set_feedback(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    investigation_id: UUID,
    verdict: str,
    note: str | None,
    user_id: UUID,
) -> bool:
    """Record the engineer's verdict. False when there is no such memory for this tenant."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    result = await session.execute(
        text(
            "UPDATE ai.investigation_memory SET feedback_verdict = :verdict, feedback_note = :note, "
            "  feedback_by = :by, updated_at = now() WHERE tenant_id = :t AND investigation_id = :inv"
        ),
        {"verdict": verdict, "note": note, "by": user_id, "t": tenant_id, "inv": investigation_id},
    )
    await session.commit()
    return result.rowcount > 0


async def search(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    query_vector: list[float],
    embedding_model: str,
    exclude_incident_id: UUID | None,
    limit: int,
) -> list[MemoryRow]:
    """The memories nearest the query, closest first: this tenant, this embedding model, not the incident being investigated."""
    result = await session.execute(
        text(
            f"SELECT {_COLUMNS}, embedding <=> CAST(:v AS vector) AS distance "
            "FROM ai.investigation_memory "
            "WHERE tenant_id = :t AND embedding_model = :model "
            "  AND (CAST(:exclude AS uuid) IS NULL OR incident_id <> CAST(:exclude AS uuid)) "
            "ORDER BY embedding <=> CAST(:v AS vector) LIMIT :limit"
        ),
        {
            "v": to_pgvector(query_vector),
            "t": tenant_id,
            "model": embedding_model,
            "exclude": exclude_incident_id,
            "limit": limit,
        },
    )
    rows = []
    for r in result.all():
        base = _row(r)
        rows.append(MemoryRow(**{**base.__dict__, "distance": float(r.distance)}))
    return rows
