"""Persistence for ai.runbook and ai.runbook_chunk (runbooks and their searchable chunks).

Every function takes ``tenant_id`` and puts it in the WHERE clause; a search never returns another
tenant's chunk. Vectors are written and compared through ``CAST(:v AS vector)`` with the text form
from ``to_pgvector``, so no extra driver package is needed.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.embeddings import to_pgvector
from app.retrieval.splitter import Chunk


@dataclass(frozen=True)
class Runbook:
    id: UUID
    tenant_id: UUID
    title: str
    content_hash: str
    embedding_model: str
    chunk_count: int
    created_by: UUID
    created_at: datetime
    source_kind: str = "markdown"


@dataclass(frozen=True)
class RunbookSource:
    """What the viewer shows: the text, and for a PDF also the original file."""

    id: UUID
    title: str
    kind: str  # "markdown", "text", "pdf" or "docx"
    text: str  # the text as uploaded, or the text extracted from the file
    file: bytes | None
    reconstructed: bool  # True when the text was rebuilt from the index (stored before originals were kept)


@dataclass(frozen=True)
class ChunkHit:
    """A search result. ``distance`` is cosine distance: 0 is the same direction, 1 unrelated, 2 opposite."""

    chunk_id: UUID
    runbook_id: UUID
    runbook_title: str
    ordinal: int
    heading_path: str
    content: str
    distance: float
    embedding: list[float]  # kept so the caller can pick diverse results (MMR) without asking again


def _runbook(row) -> Runbook:
    m = row._mapping
    return Runbook(**{f: m[f] for f in Runbook.__dataclass_fields__})


_COLUMNS = "id, tenant_id, title, content_hash, embedding_model, chunk_count, created_by, created_at, source_kind"


async def find_by_title(session: AsyncSession, *, tenant_id: UUID, title: str) -> Runbook | None:
    result = await session.execute(
        text(f"SELECT {_COLUMNS} FROM ai.runbook WHERE tenant_id = :t AND title = :title"),
        {"t": tenant_id, "title": title},
    )
    row = result.first()
    return _runbook(row) if row else None


async def list_for_tenant(session: AsyncSession, *, tenant_id: UUID) -> list[Runbook]:
    result = await session.execute(
        text(f"SELECT {_COLUMNS} FROM ai.runbook WHERE tenant_id = :t ORDER BY title"), {"t": tenant_id}
    )
    return [_runbook(row) for row in result.all()]


async def replace(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    title: str,
    content_hash: str,
    embedding_model: str,
    created_by: UUID,
    chunks: list[Chunk],
    vectors: list[list[float]],
    source_text: str | None = None,
    source_file: bytes | None = None,
    source_kind: str = "markdown",
) -> Runbook:
    """Store a runbook, replacing the one with the same title. Old and new chunks are never both visible:
    this is one transaction."""
    if len(chunks) != len(vectors):
        raise ValueError("one vector per chunk is required")
    try:
        await session.execute(
            text("DELETE FROM ai.runbook WHERE tenant_id = :t AND title = :title"), {"t": tenant_id, "title": title}
        )
        result = await session.execute(
            text(
                "INSERT INTO ai.runbook (tenant_id, title, content_hash, embedding_model, chunk_count, created_by, "
                "source_kind, source_text, source_file) "
                f"VALUES (:t, :title, :hash, :model, :count, :by, :kind, :src, :file) RETURNING {_COLUMNS}"
            ),
            {
                "t": tenant_id,
                "title": title,
                "hash": content_hash,
                "model": embedding_model,
                "count": len(chunks),
                "by": created_by,
                "kind": source_kind,
                "src": source_text,
                "file": source_file,
            },
        )
        runbook = _runbook(result.one())
        if chunks:
            await session.execute(
                text(
                    "INSERT INTO ai.runbook_chunk (runbook_id, tenant_id, ordinal, heading_path, content, embedding) "
                    "VALUES (:rb, :t, :ord, :path, :content, CAST(:v AS vector))"
                ),
                [
                    {
                        "rb": runbook.id,
                        "t": tenant_id,
                        "ord": c.ordinal,
                        "path": c.heading_path,
                        "content": c.content,
                        "v": to_pgvector(vec),
                    }
                    for c, vec in zip(chunks, vectors, strict=True)
                ],
            )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return runbook


async def keep_source_if_missing(
    session: AsyncSession, *, tenant_id: UUID, runbook_id: UUID, source_text: str | None, source_file: bytes | None
) -> None:
    """Runbooks stored before originals were kept get theirs the next time the same text is uploaded."""
    await session.execute(
        text(
            "UPDATE ai.runbook SET source_text = :src, source_file = :file "
            "WHERE tenant_id = :t AND id = :id AND source_text IS NULL AND source_file IS NULL"
        ),
        {"t": tenant_id, "id": runbook_id, "src": source_text, "file": source_file},
    )
    await session.commit()


async def get_source(session: AsyncSession, *, tenant_id: UUID, runbook_id: UUID) -> RunbookSource | None:
    """The runbook as it was uploaded, for the viewer. A runbook stored before originals were kept is rebuilt from
    its indexed chunks (headings and text in order) and marked ``reconstructed``."""
    result = await session.execute(
        text("SELECT id, title, source_kind, source_text, source_file FROM ai.runbook WHERE tenant_id = :t AND id = :id"),
        {"t": tenant_id, "id": runbook_id},
    )
    row = result.first()
    if row is None:
        return None
    m = row._mapping
    if m["source_text"] is not None or m["source_file"] is not None:
        return RunbookSource(
            id=m["id"],
            title=m["title"],
            kind=m["source_kind"],
            text=m["source_text"] or "",
            file=bytes(m["source_file"]) if m["source_file"] is not None else None,
            reconstructed=False,
        )
    chunks = await session.execute(
        text(
            "SELECT heading_path, content FROM ai.runbook_chunk "
            "WHERE tenant_id = :t AND runbook_id = :id ORDER BY ordinal"
        ),
        {"t": tenant_id, "id": runbook_id},
    )
    parts: list[str] = []
    last_path = None
    for path, content in ((r[0], r[1]) for r in chunks.all()):
        if path and path != last_path:
            level = path.count(" > ") + 1
            parts.append(f"{'#' * min(level, 4)} {path.split(' > ')[-1]}")
            last_path = path
        parts.append(content)
    return RunbookSource(
        id=m["id"], title=m["title"], kind="markdown", text="\n\n".join(parts), file=None, reconstructed=True
    )


async def delete_one(session: AsyncSession, *, tenant_id: UUID, runbook_id: UUID) -> bool:
    """True if removed (its chunks go with it: ON DELETE CASCADE)."""
    result = await session.execute(
        text("DELETE FROM ai.runbook WHERE tenant_id = :t AND id = :id"), {"t": tenant_id, "id": runbook_id}
    )
    await session.commit()
    return result.rowcount > 0


async def search(
    session: AsyncSession, *, tenant_id: UUID, query_vector: list[float], limit: int, embedding_model: str
) -> list[ChunkHit]:
    """The ``limit`` chunks nearest to the query among this tenant's runbooks made with ``embedding_model``."""
    result = await session.execute(
        text(
            "SELECT c.id, c.runbook_id, r.title, c.ordinal, c.heading_path, c.content, "
            "       c.embedding <=> CAST(:v AS vector) AS distance, c.embedding::text AS embedding "
            "FROM ai.runbook_chunk c JOIN ai.runbook r ON r.id = c.runbook_id "
            "WHERE c.tenant_id = :t AND r.tenant_id = :t AND r.embedding_model = :model "
            "ORDER BY c.embedding <=> CAST(:v AS vector) LIMIT :limit"
        ),
        {"v": to_pgvector(query_vector), "t": tenant_id, "model": embedding_model, "limit": limit},
    )
    return [
        ChunkHit(
            chunk_id=row.id,
            runbook_id=row.runbook_id,
            runbook_title=row.title,
            ordinal=row.ordinal,
            heading_path=row.heading_path,
            content=row.content,
            distance=float(row.distance),
            embedding=[float(x) for x in row.embedding.strip("[]").split(",")],
        )
        for row in result.all()
    ]
