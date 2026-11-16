"""Fixture backends: everything an investigation reads, replayed from files and a git repository.

An evaluation must not need ClickHouse, the incident database, GitHub or a running shop. Each backend the
graph uses has a stand-in here that answers from recorded data:

* ``FixtureLogs``      the log store; rows recorded per query kind from a real run (see ``capture.py``)
* ``FixtureIncidents`` the incident-service API; one recorded incident
* ``FixtureHistory``   earlier incidents of the tenant; recorded rows
* ``RepoCode``         the tenant's repository: a git worktree of one ref of a real repository
* ``FixtureRunbooks``  runbook search over Markdown files, in memory (no database, no model)

The recorded data is real: it is exactly what the production tools returned for the incident, so the nodes
under test (``load_context``, ``gather_logs``, the stack-trace parser, the code tools) run unchanged.
The ``Recording*`` wrappers are the other half: put them around the real backends once, run the real nodes, and
the fixture files are written from what they saw.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from app.graph.code_access import CodeCheckout
from app.retrieval.embeddings import HashingEmbedder
from app.retrieval.runbooks import (
    FETCH_K,
    MAX_DISTANCE,
    K,
    RunbookPassage,
    RunbookSearchResult,
    mmr,
)
from app.retrieval.splitter import split_markdown
from app.schemas.domain import RepoContext
from app.store.runbooks import ChunkHit
from app.tools.code import CodeTools
from app.tools.git import GitTools

# every fixture is replayed for this tenant, whatever tenant it was recorded under
EVAL_TENANT = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


# ------------------------------------------------------------------------- JSON for rows


def to_jsonable(value: Any) -> Any:
    """Rows from ClickHouse and Postgres as plain JSON: datetimes become ``{"$dt": iso}`` so they come back as datetimes."""
    if isinstance(value, datetime):
        return {"$dt": (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    return value


def from_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$dt"}:
            return datetime.fromisoformat(value["$dt"])
        return {k: from_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_jsonable(v) for v in value]
    return value


def classify_log_query(sql: str) -> str:
    """Which of log0's five ClickHouse queries this is. They are recognised by a marker in their SQL, which is
    how both the recorder and the replay agree on the key."""
    if "stack_trace" in sql:
        return "trace"
    if "toStartOfInterval" in sql:
        return "timeline"
    if "GROUP BY service_name, fingerprint" in sql:
        return "cooccurring"
    if "ORDER BY timestamp DESC" in sql:
        return "search"
    return "stats"


# --------------------------------------------------------------------------- recording


class RecordingLogs:
    """Wraps the real log store and remembers the rows of every query, by kind (the last one of each kind wins,
    except ``search``, where every distinct result is kept: the investigation may run several searches)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.rows: dict[str, Any] = {}

    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = await self._inner.query(sql, tenant_id=tenant_id, parameters=parameters)
        kind = classify_log_query(sql)
        if kind == "search":
            self.rows.setdefault("search", [])
            if result:
                self.rows["search"] = result if len(result) >= len(self.rows["search"]) else self.rows["search"]
        else:
            self.rows[kind] = result
        return result


class RecordingHistory:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.rows: list[dict[str, Any]] = []

    async def find_related(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.rows = await self._inner.find_related(**kwargs)
        return self.rows


class RecordingIncidents:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.payload: dict[str, Any] | None = None

    async def get_incident(self, incident_id: UUID, tenant_id: UUID) -> dict[str, Any]:
        self.payload = await self._inner.get_incident(incident_id, tenant_id)
        return self.payload


# ----------------------------------------------------------------------------- replay


class FixtureLogs:
    """Replays recorded rows by query kind. ``inject`` adds one extra log line to every search and to the
    incident's context (used by the prompt-injection canary case)."""

    def __init__(self, rows: dict[str, Any], *, inject: str | None = None) -> None:
        self.rows = from_jsonable(rows)
        self.inject = inject
        self.calls: list[str] = []

    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        kind = classify_log_query(sql)
        self.calls.append(kind)
        rows = self.rows.get(kind, [])
        if kind == "trace" and not rows:
            return []
        if kind == "search" and self.inject:
            template = rows[0] if rows else {"timestamp": datetime.now(UTC), "service_name": "storefront-api", "level": "ERROR", "fingerprint": "0" * 64}
            return [{**template, "message": self.inject}, *rows]
        return list(rows) if isinstance(rows, list) else [rows]


class FixtureIncidents:
    def __init__(self, payload: dict[str, Any], *, inject: str | None = None) -> None:
        self.payload = {**payload, "tenantId": str(EVAL_TENANT)}
        if inject:
            self.payload["topMessages"] = [inject, *(self.payload.get("topMessages") or [])]
        self.calls = 0

    async def get_incident(self, incident_id: UUID, tenant_id: UUID) -> dict[str, Any]:
        self.calls += 1
        return self.payload


class FixtureHistory:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = from_jsonable(rows)

    async def find_related(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.rows)


# ---------------------------------------------------------------------------- the code


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


@dataclass(frozen=True)
class RecordingCodeTools(CodeTools):
    """The real code tools, remembering which files were read (the safety evaluator checks for ``.env``)."""

    reads: list[str] = field(default_factory=list, compare=False)

    async def read_file(self, path: str, start_line: int = 1, end_line: int | None = None):  # type: ignore[override]
        self.reads.append(path)
        return await super().read_file(path, start_line, end_line)


class RepoCode:
    """``CodeAccess`` over one ref of a real git repository, via ``git worktree`` (full history is available, so
    ``recent_commits``, ``blame`` and ``show_commit`` are real). Service names map to folders: ``services`` is
    ``{"payments": "services/payments", ...}``."""

    def __init__(self, repo: Path, ref: str, services: dict[str, str], *, repo_name: str = "log0labs/log0-demo-shop") -> None:
        self.repo = repo
        self.ref = ref
        self.services = services
        self.repo_name = repo_name
        self.sha = git(repo, "rev-parse", f"{ref}^{{commit}}")
        self.reads: list[str] = []
        self._worktrees: list[Path] = []

    def _worktree(self, sha: str) -> Path:
        path = Path(tempfile.mkdtemp(prefix="eval-wt-"))
        git(self.repo, "worktree", "add", "--detach", "--force", str(path), sha)
        self._worktrees.append(path)
        return path

    def _checkout(self, root: Path, service_name: str, sha: str) -> CodeCheckout:
        prefix = self.services[service_name]
        return CodeCheckout(
            repo=RepoContext(repo=self.repo_name, branch=self.ref, sha=sha, path_prefix=prefix),
            code=RecordingCodeTools(root, prefix=prefix, reads=self.reads),
            git=GitTools(root, prefix=prefix),
        )

    async def open(self, *, tenant_id: UUID, service_name: str) -> CodeCheckout | None:
        if service_name not in self.services:
            return None
        return self._checkout(self._worktree(self.sha), service_name, self.sha)

    @asynccontextmanager
    async def snapshot(self, *, tenant_id: UUID, service_name: str, sha: str) -> AsyncIterator[CodeCheckout | None]:
        if service_name not in self.services:
            yield None
            return
        root = self._worktree(sha)
        yield self._checkout(root, service_name, sha)

    def close(self) -> None:
        for path in self._worktrees:
            subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=self.repo, capture_output=True, check=False)
        self._worktrees.clear()
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True, check=False)

    @contextmanager
    def managed(self):
        try:
            yield self
        finally:
            self.close()


# -------------------------------------------------------------------------- runbooks


class FixtureRunbooks:
    """Runbook search over Markdown files, in memory: the same splitter, hashing embedder, distance floor and MMR as
    production, without the database and without the model grader (so retrieval alone can be measured)."""

    def __init__(self, directory: Path) -> None:
        self._embedder = HashingEmbedder()
        self._directory = directory
        self._chunks: list[tuple[str, Any]] = []  # (title, Chunk)
        self._vectors: list[list[float]] = []
        self._ready = False

    async def _load(self) -> None:
        if self._ready:
            return
        for file in sorted(self._directory.glob("*.md")):
            for chunk in split_markdown(file.read_text(encoding="utf-8")):
                self._chunks.append((file.stem, chunk))
        self._vectors = await self._embedder.embed_documents([c.embed_text for _, c in self._chunks])
        self._ready = True

    async def retrieve(self, query: str) -> list[RunbookPassage]:
        await self._load()
        query_vector = await self._embedder.embed_query(query)
        scored = []
        for index, ((title, chunk), vector) in enumerate(zip(self._chunks, self._vectors, strict=True)):
            distance = 1.0 - sum(a * b for a, b in zip(query_vector, vector, strict=True))
            scored.append(
                ChunkHit(
                    chunk_id=UUID(int=index + 1),
                    runbook_id=UUID(int=0),
                    runbook_title=title,
                    ordinal=chunk.ordinal,
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    distance=distance,
                    embedding=vector,
                )
            )
        scored.sort(key=lambda h: h.distance)
        close = [h for h in scored[:FETCH_K] if h.distance <= MAX_DISTANCE]
        return [
            RunbookPassage(
                chunk_id=h.chunk_id, runbook_id=h.runbook_id, title=h.runbook_title, heading_path=h.heading_path,
                ordinal=h.ordinal, content=h.content, distance=h.distance,
            )
            for h in mmr(close, K)
        ]

    async def search(self, *, tenant_id: UUID, query: str, models: Any) -> RunbookSearchResult:
        await self._load()
        if not self._chunks:
            return RunbookSearchResult(outcome="no_runbooks", query_used=query)
        passages = await self.retrieve(query)
        if not passages:
            return RunbookSearchResult(outcome="none_relevant", query_used=query, graded=False)
        return RunbookSearchResult(outcome="found", passages=passages, query_used=query, graded=False)


# ------------------------------------------------------------------------- fixture files


def save_fixture(directory: Path, *, incident: dict[str, Any], logs: dict[str, Any], history: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in (("incident.json", incident), ("logs.json", logs), ("history.json", history)):
        (directory / name).write_text(json.dumps(to_jsonable(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_fixture(directory: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    read = lambda name: json.loads((directory / name).read_text(encoding="utf-8"))
    return read("incident.json"), read("logs.json"), read("history.json")
