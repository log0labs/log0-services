"""``/api/v1/runbooks``: the team's Markdown runbooks that investigations can search.

Anyone signed in can list; only an ADMIN can upload or delete. An upload is split by headings
(``retrieval/splitter.py``), embedded, and stored with the tenant id from the token. Uploading a
title that already exists replaces it; uploading the same text again changes nothing and does not
call the embedding service.
"""

import hashlib
import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.retrieval.embeddings import Embedder, EmbeddingError
from app.retrieval.splitter import split_markdown
from app.schemas.base import CamelModel
from app.security.jwt import CurrentUser, get_current_user, require_admin
from app.store import runbooks as store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/runbooks", tags=["runbooks"])

MAX_MARKDOWN_CHARS = 200_000
MAX_CHUNKS = 300


def get_embedder(request: Request) -> Embedder:
    return request.app.state.embedder


class RunbookIn(CamelModel):
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=MAX_MARKDOWN_CHARS)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("title must be plain text")
        return value


class RunbookOut(CamelModel):
    id: UUID
    title: str
    chunk_count: int
    embedding_model: str
    created_at: datetime


def _out(row: store.Runbook) -> RunbookOut:
    return RunbookOut(
        id=row.id,
        title=row.title,
        chunk_count=row.chunk_count,
        embedding_model=row.embedding_model,
        created_at=row.created_at,
    )


@router.post("", response_model=RunbookOut, status_code=status.HTTP_201_CREATED)
async def upload_runbook(
    body: RunbookIn,
    response: Response,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> RunbookOut:
    content_hash = hashlib.sha256(body.markdown.encode()).hexdigest()
    existing = await store.find_by_title(session, tenant_id=admin.tenant_id, title=body.title)
    if existing and existing.content_hash == content_hash and existing.embedding_model == embedder.model_id:
        response.status_code = status.HTTP_200_OK  # nothing to do
        return _out(existing)

    chunks = split_markdown(body.markdown)
    if not chunks:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The runbook has no text to index")
    if len(chunks) > MAX_CHUNKS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"The runbook is too long ({len(chunks)} parts, at most {MAX_CHUNKS}); split it into several runbooks",
        )
    try:
        vectors = await embedder.embed_documents([c.embed_text for c in chunks])
    except EmbeddingError as exc:
        logger.warning("runbook embedding failed: tenant=%s: %s", admin.tenant_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The embedding service is not available, try again later") from None

    row = await store.replace(
        session,
        tenant_id=admin.tenant_id,
        title=body.title,
        content_hash=content_hash,
        embedding_model=embedder.model_id,
        created_by=admin.user_id,
        chunks=chunks,
        vectors=vectors,
    )
    logger.info("runbook stored: tenant=%s chunks=%d", admin.tenant_id, row.chunk_count)
    return _out(row)


@router.get("", response_model=list[RunbookOut])
async def list_runbooks(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[RunbookOut]:
    return [_out(r) for r in await store.list_for_tenant(session, tenant_id=user.tenant_id)]


@router.delete("/{runbook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runbook(
    runbook_id: UUID,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if not await store.delete_one(session, tenant_id=admin.tenant_id, runbook_id=runbook_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
