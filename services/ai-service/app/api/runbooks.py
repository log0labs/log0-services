"""``/api/v1/runbooks``: the team's Markdown runbooks that investigations can search.

Anyone signed in can list; only an ADMIN can upload or delete. An upload is split by headings
(``retrieval/splitter.py``), embedded, and stored with the tenant id from the token. Uploading a
title that already exists replaces it; uploading the same text again changes nothing and does not
call the embedding service.
"""

import base64
import binascii
import hashlib
import io
import logging
import zipfile
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from docx import Document
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import Field, field_validator, model_validator
from pypdf import PdfReader
from pypdf.errors import PyPdfError
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
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DOCX_UNPACKED_BYTES = 50 * 1024 * 1024


def get_embedder(request: Request) -> Embedder:
    return request.app.state.embedder


class RunbookIn(CamelModel):
    """A runbook as text (Markdown or plain), or as a PDF or Word file (base64) whose text is extracted and indexed."""

    title: str = Field(min_length=1, max_length=200)
    markdown: str | None = Field(default=None, min_length=1, max_length=MAX_MARKDOWN_CHARS)
    format: Literal["markdown", "text"] = "markdown"
    file_base64: str | None = Field(default=None, max_length=(MAX_FILE_BYTES * 4) // 3 + 8)
    file_type: Literal["pdf", "docx"] | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("title must be plain text")
        return value

    @model_validator(mode="after")
    def _one_source(self) -> "RunbookIn":
        if (self.markdown is None) == (self.file_base64 is None):
            raise ValueError("send either markdown or fileBase64")
        if self.file_base64 is not None and self.file_type is None:
            raise ValueError("fileBase64 needs fileType (pdf or docx)")
        return self


def _pdf_text(data: bytes) -> str:
    """The text layer of a PDF, page by page. 422 if it is not a readable PDF or has no text (a scan)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The PDF is password protected")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except HTTPException:
        raise
    except (PyPdfError, ValueError, OSError, KeyError, TypeError, RecursionError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That file is not a readable PDF") from None
    text = "\n\n".join(p for p in pages if p)
    if not text:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The PDF has no text to index (a scanned image has no text layer)"
        )
    return text[:MAX_MARKDOWN_CHARS]


def _docx_text(data: bytes) -> str:
    """A Word file as Markdown: headings keep their level (so the runbook splits by section), list items become
    bullets and table rows become lines. 422 if it is not a readable .docx or has no text."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 2000 or sum(i.file_size for i in infos) > MAX_DOCX_UNPACKED_BYTES:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The Word file is too large when unpacked")
        document = Document(io.BytesIO(data))
        lines: list[str] = []
        for block in document.iter_inner_content():
            if hasattr(block, "rows"):  # a table: one line per row
                for row in block.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        lines.append(" | ".join(cells))
                continue
            text = block.text.strip()
            if not text:
                continue
            style = (block.style.name if block.style is not None else "") or ""
            if style == "Title":
                lines.append(f"# {text}")
            elif style.startswith("Heading ") and style[8:].isdigit():
                lines.append(f"{'#' * min(max(int(style[8:]), 1), 4)} {text}")
            elif style.startswith("List"):
                lines.append(f"- {text}")
            else:
                lines.append(text)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - python-docx and lxml raise many types for a bad file
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That file is not a readable Word document") from None
    text = "\n\n".join(lines)
    if not text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The Word document has no text to index")
    return text[:MAX_MARKDOWN_CHARS]


class RunbookDetail(CamelModel):
    """A runbook for the viewer: its text, and whether the uploaded file (PDF or Word) can be fetched."""

    id: UUID
    title: str
    kind: str
    text: str
    has_file: bool
    reconstructed: bool


class RunbookOut(CamelModel):
    id: UUID
    title: str
    chunk_count: int
    embedding_model: str
    created_at: datetime
    source_kind: str


def _out(row: store.Runbook) -> RunbookOut:
    return RunbookOut(
        id=row.id,
        title=row.title,
        chunk_count=row.chunk_count,
        embedding_model=row.embedding_model,
        created_at=row.created_at,
        source_kind=row.source_kind,
    )


@router.post("", response_model=RunbookOut, status_code=status.HTTP_201_CREATED)
async def upload_runbook(
    body: RunbookIn,
    response: Response,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> RunbookOut:
    file_bytes: bytes | None = None
    source_kind = body.format
    if body.file_base64 is not None:
        try:
            file_bytes = base64.b64decode(body.file_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is not valid base64") from None
        if len(file_bytes) > MAX_FILE_BYTES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is larger than 5 MB")
        source_kind = body.file_type or "pdf"
        if source_kind == "pdf":
            if not file_bytes.startswith(b"%PDF-"):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That file is not a PDF")
            indexed_text = _pdf_text(file_bytes)
        else:
            if not file_bytes.startswith(b"PK"):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That file is not a Word (.docx) document")
            indexed_text = _docx_text(file_bytes)
        content_hash = hashlib.sha256(file_bytes).hexdigest()
    else:
        indexed_text = body.markdown or ""
        content_hash = hashlib.sha256(indexed_text.encode()).hexdigest()
    existing = await store.find_by_title(session, tenant_id=admin.tenant_id, title=body.title)
    if existing and existing.content_hash == content_hash and existing.embedding_model == embedder.model_id:
        await store.keep_source_if_missing(
            session, tenant_id=admin.tenant_id, runbook_id=existing.id, source_text=indexed_text, source_file=file_bytes
        )
        response.status_code = status.HTTP_200_OK  # nothing to do
        return _out(existing)

    chunks = split_markdown(indexed_text)
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
        source_text=indexed_text,
        source_file=file_bytes,
        source_kind=source_kind,
    )
    logger.info("runbook stored: tenant=%s chunks=%d", admin.tenant_id, row.chunk_count)
    return _out(row)


@router.get("", response_model=list[RunbookOut])
async def list_runbooks(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[RunbookOut]:
    return [_out(r) for r in await store.list_for_tenant(session, tenant_id=user.tenant_id)]


@router.get("/{runbook_id}", response_model=RunbookDetail)
async def get_runbook(
    runbook_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RunbookDetail:
    source = await store.get_source(session, tenant_id=user.tenant_id, runbook_id=runbook_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Runbook not found")
    return RunbookDetail(
        id=source.id,
        title=source.title,
        kind=source.kind,
        text=source.text,
        has_file=source.file is not None,
        reconstructed=source.reconstructed,
    )


_FILE_TYPES = {
    "pdf": ("application/pdf", "inline"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "attachment"),
}


@router.get("/{runbook_id}/file")
async def get_runbook_file(
    runbook_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The file as uploaded (PDF or Word). 404 for a text runbook, which has no file."""
    source = await store.get_source(session, tenant_id=user.tenant_id, runbook_id=runbook_id)
    if source is None or source.file is None or source.kind not in _FILE_TYPES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No file for this runbook")
    media_type, disposition = _FILE_TYPES[source.kind]
    return Response(
        content=source.file,
        media_type=media_type,
        headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{runbook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runbook(
    runbook_id: UUID,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if not await store.delete_one(session, tenant_id=admin.tenant_id, runbook_id=runbook_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
