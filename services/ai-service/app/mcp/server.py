"""log0's tools as an MCP server, so Claude Desktop, Cursor or any MCP client can ask about your incidents.

MCP is an adapter over the tool layer the investigation graph already uses (``app/tools``): no new
logic, no new data access. Everything here is read-only, and **the tenant is never an argument**: it
comes from the verified token of whoever is connected (``current_user``), so a client cannot ask
about another tenant however it phrases the call.

The tools (all read-only):
    get_incident, get_error_timeline, get_cooccurring_errors, search_logs, find_related_incidents,
    search_runbooks, get_investigation, search_code, read_file, recent_commits

Failures are turned into short, safe messages (never a stack trace, a path on the server, or a secret).
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.graph.code_access import CodeAccess, CodeAccessError, CodeCheckout
from app.graph.nodes.gather_logs import pick_bucket
from app.graph.nodes.load_context import investigation_window
from app.integrations.clickhouse import ClickHouseError
from app.integrations.incident_service import IncidentNotFoundError
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.runbooks import RunbookSource
from app.security.jwt import CurrentUser
from app.security.redaction import redact_value
from app.store import investigations as investigation_store
from app.tools.code import CodeToolError
from app.tools.incidents import (
    IncidentApi,
    RelatedIncidentSource,
    find_related_incidents,
    get_incident,
)
from app.tools.logs import (
    LogBackend,
    get_cooccurring_errors,
    get_error_timeline,
    search_logs,
)

logger = logging.getLogger(__name__)

# Who is calling. The HTTP endpoint sets it per request from the bearer token; the stdio entry point sets it
# once from the token it was started with. No user: no tool runs.
current_user: ContextVar[CurrentUser | None] = ContextVar("mcp_current_user", default=None)

# When the signed-in session ends (epoch seconds). Only the stdio entry point sets it: one token serves a whole
# session there, so the tools check it on every call. Over HTTP every request carries its own token.
current_deadline: ContextVar[float | None] = ContextVar("mcp_deadline", default=None)

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
MAX_RUNBOOK_RESULTS = 8


class ToolFailure(ToolError):
    """A problem the client may see: the message is written to be shown. (Only a ``ToolError`` reaches the client
    with its text; any other exception is hidden behind a generic message, which is what we want for bugs.)"""


@dataclass(frozen=True)
class McpBackends:
    logs: LogBackend
    incidents: IncidentApi
    history: RelatedIncidentSource
    session_factory: async_sessionmaker[AsyncSession]
    runbooks: RunbookSource | None = None
    code: CodeAccess | None = None


def tenant_id() -> UUID:
    user = current_user.get()
    if user is None:
        raise ToolFailure("Not authenticated")
    deadline = current_deadline.get()
    if deadline is not None and time.time() >= deadline:
        raise ToolFailure("The token has expired: create a new one and restart the client")
    return user.tenant_id


def parse_uuid(value: str, name: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ToolFailure(f"{name} must be a UUID like 3f2a9c1e-77aa-4b1c-9d00-0123456789ab") from None


def parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, AttributeError):
        raise ToolFailure(f"{name} must be an ISO 8601 time like 2026-11-15T10:30:00Z") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def guard[F: Callable[..., Awaitable[Any]]](fn: F) -> F:
    """Turn every way a tool can fail into a message that is safe to show, and keep the real cause in the log."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            # the client's model is another vendor's: it gets the same redacted view the investigation graph has
            return redact_value(await fn(*args, **kwargs))
        except ToolFailure:
            raise
        except IncidentNotFoundError:
            raise ToolFailure("Incident not found") from None
        except ValueError as exc:  # our own range and format checks: their messages are written for users
            raise ToolFailure(str(exc)) from None
        except (CodeToolError, CodeAccessError) as exc:  # their messages are written to be shown
            raise ToolFailure(str(exc)) from None
        except ClickHouseError:
            logger.warning("mcp tool %s: log store error", fn.__name__, exc_info=True)
            raise ToolFailure("The log store is not available right now") from None
        except EmbeddingError:
            logger.warning("mcp tool %s: embedding error", fn.__name__, exc_info=True)
            raise ToolFailure("Runbook search is not available right now") from None
        except (SQLAlchemyError, OSError):
            logger.warning("mcp tool %s: database error", fn.__name__, exc_info=True)
            raise ToolFailure("The database is not available right now") from None
        except Exception:
            logger.exception("mcp tool %s failed", fn.__name__)
            raise ToolFailure("The tool failed unexpectedly") from None

    return wrapper  # type: ignore[return-value]


def build_mcp_server(backends: McpBackends) -> MCPServer:
    mcp = MCPServer(
        "log0",
        instructions=(
            "log0 is an incident platform. These read-only tools look up incidents, their logs, past incidents, "
            "runbooks, AI investigations and the connected code. Start with get_incident, then look at the error "
            "timeline and co-occurring errors; use search_code and read_file only for a service that has a repository."
        ),
    )

    async def incident(incident_id: str):
        return await get_incident(backends.incidents, tenant_id=tenant_id(), incident_id=parse_uuid(incident_id, "incident_id"))

    async def checkout(service_name: str) -> CodeCheckout:
        if backends.code is None:
            raise ToolFailure("Code access is not configured on this server")
        opened = await backends.code.open(tenant_id=tenant_id(), service_name=service_name)
        if opened is None:
            raise ToolFailure(f"No repository is connected to the service {service_name!r}")
        return opened

    @mcp.tool(name="get_incident", annotations=_READ_ONLY)
    @guard
    async def get_incident_tool(incident_id: str) -> dict[str, Any]:
        """The incident: service, severity, status, occurrence count, first and last seen, top messages, AI summary."""
        return (await incident(incident_id)).model_dump(mode="json")

    @mcp.tool(name="get_error_timeline", annotations=_READ_ONLY)
    @guard
    async def get_error_timeline_tool(incident_id: str) -> dict[str, Any]:
        """How often this incident's error occurred over time (per-bucket counts, from 30 minutes before it began)."""
        found = await incident(incident_id)
        start, end = investigation_window(found)
        timeline = await get_error_timeline(
            backends.logs, tenant_id=tenant_id(), fingerprint=found.fingerprint, start=start, end=end, bucket=pick_bucket(start, end)
        )
        return timeline.model_dump(mode="json")

    @mcp.tool(name="get_cooccurring_errors", annotations=_READ_ONLY)
    @guard
    async def get_cooccurring_errors_tool(incident_id: str) -> dict[str, Any]:
        """What else was failing, in any service, while this incident happened (points at upstream causes or a wider outage)."""
        found = await incident(incident_id)
        start, end = investigation_window(found)
        errors = await get_cooccurring_errors(
            backends.logs, tenant_id=tenant_id(), start=start, end=end, exclude_fingerprint=found.fingerprint, limit=10
        )
        return {"errors": [e.model_dump(mode="json") for e in errors]}

    @mcp.tool(name="search_logs", annotations=_READ_ONLY)
    @guard
    async def search_logs_tool(
        start: str, end: str, query: str | None = None, service: str | None = None, level: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Search log lines between two ISO times (at most 7 days apart) by text, service and level. Exact filters, not semantic."""
        result = await search_logs(
            backends.logs,
            tenant_id=tenant_id(),
            start=parse_time(start, "start"),
            end=parse_time(end, "end"),
            query=query,
            service=service,
            level=level,
            limit=limit,
        )
        return result.model_dump(mode="json")

    @mcp.tool(name="find_related_incidents", annotations=_READ_ONLY)
    @guard
    async def find_related_incidents_tool(incident_id: str) -> dict[str, Any]:
        """Earlier incidents with the same error, then other incidents of the same service, from the last 30 days."""
        found = await incident(incident_id)
        related = await find_related_incidents(backends.history, tenant_id=tenant_id(), incident=found, limit=10)
        return {"incidents": [r.model_dump(mode="json") for r in related]}

    @mcp.tool(name="search_runbooks", annotations=_READ_ONLY)
    @guard
    async def search_runbooks_tool(query: str) -> dict[str, Any]:
        """Search the team's runbooks for passages that help with a problem described in plain words."""
        if backends.runbooks is None:
            raise ToolFailure("Runbook search is not configured on this server")
        query = query.strip()
        if not query:
            raise ToolFailure("query must not be empty")
        result = await backends.runbooks.search(tenant_id=tenant_id(), query=query, models=None)
        return {
            "outcome": result.outcome,
            "passages": [
                {"runbook": p.title, "section": p.heading_path, "content": p.content, "distance": round(p.distance, 3)}
                for p in result.passages[:MAX_RUNBOOK_RESULTS]
            ],
        }

    @mcp.tool(name="get_investigation", annotations=_READ_ONLY)
    @guard
    async def get_investigation_tool(investigation_id: str) -> dict[str, Any]:
        """An AI investigation: status, root cause, recommendation, confidence and the proposed diff (when finished)."""
        async with backends.session_factory() as session:
            row = await investigation_store.get_by_id(
                session, tenant_id=tenant_id(), investigation_id=parse_uuid(investigation_id, "investigation_id")
            )
        if row is None:
            raise ToolFailure("Investigation not found")
        return row.model_dump(mode="json", by_alias=True)

    @mcp.tool(name="search_code", annotations=_READ_ONLY)
    @guard
    async def search_code_tool(service_name: str, pattern: str, glob: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Search a service's connected repository for a regular expression (optionally only files matching a glob like *.py)."""
        opened = await checkout(service_name)
        result = await opened.code.search_code(pattern, glob, limit=max(1, min(limit, 50)))
        return {"repository": opened.repo.repo, "commit": opened.repo.sha, **result.model_dump(mode="json")}

    @mcp.tool(name="read_file", annotations=_READ_ONLY)
    @guard
    async def read_file_tool(service_name: str, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        """Read a file (or a line range) of a service's connected repository. Files that may hold secrets are refused."""
        opened = await checkout(service_name)
        result = await opened.code.read_file(path, start_line, end_line)
        return {"repository": opened.repo.repo, "commit": opened.repo.sha, **result.model_dump(mode="json")}

    @mcp.tool(name="recent_commits", annotations=_READ_ONLY)
    @guard
    async def recent_commits_tool(service_name: str, since: str | None = None, until: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Recent commits touching a service's folder (newest first), optionally between two ISO times. The usual change signal."""
        opened = await checkout(service_name)
        result = await opened.git.recent_commits(
            since=parse_time(since, "since") if since else None,
            until=parse_time(until, "until") if until else None,
            limit=limit,
        )
        return result.model_dump(mode="json")

    return mcp
