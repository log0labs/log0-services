"""Step 5.5: the MCP tools, called through a real in-memory MCP client (no network, no model)."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from mcp import Client
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.session import create_session_factory
from app.graph.code_access import CodeAccessError
from app.integrations.clickhouse import ClickHouseError
from app.mcp.server import McpBackends, build_mcp_server, current_user
from app.retrieval.runbooks import RunbookPassage, RunbookSearchResult
from app.security.jwt import CurrentUser, Role
from tests.code_graph_support import FakeCode, Repo, checkout_for
from tests.conftest import requires_postgres
from tests.graph_fakes import (
    INCIDENT_ID,
    OTHER_TENANT,
    TENANT,
    FakeApi,
    FakeHistory,
    FakeLogs,
    FakeRunbooks,
    incident_payload,
)
from tests.memory_support import make_investigation, wipe_investigations

pytestmark = pytest.mark.asyncio

TOOLS = {
    "get_incident",
    "get_error_timeline",
    "get_cooccurring_errors",
    "search_logs",
    "find_related_incidents",
    "search_runbooks",
    "get_investigation",
    "search_code",
    "read_file",
    "recent_commits",
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("mcp") / "repo")


def user(tenant=TENANT, role: Role = Role.VIEWER) -> CurrentUser:
    return CurrentUser(user_id=uuid4(), tenant_id=tenant, role=role)


class World:
    def __init__(self, repo: Repo) -> None:
        self.api = FakeApi()
        self.logs = FakeLogs()
        self.history = FakeHistory()
        self.runbooks = FakeRunbooks(RunbookSearchResult(outcome="no_runbooks"))
        self.code = FakeCode(checkout_for(repo))
        self.engine = create_async_engine(get_settings().database_url_async)
        self.backends = McpBackends(
            logs=self.logs,
            incidents=self.api,
            history=self.history,
            session_factory=create_session_factory(self.engine),
            runbooks=self.runbooks,
            code=self.code,
        )


@pytest_asyncio.fixture
async def world(repo: Repo) -> AsyncIterator[World]:
    w = World(repo)
    token = current_user.set(user())
    yield w
    current_user.reset(token)
    await w.engine.dispose()


async def call(w: World, name: str, args: dict[str, Any] | None = None, *, backends: McpBackends | None = None):
    """(is_error, text) of one tool call."""
    async with Client(build_mcp_server(backends or w.backends)) as client:
        result = await client.call_tool(name, args or {})
    text = result.content[0].text if result.content else ""
    return result.is_error, text


async def ok(w: World, name: str, args: dict[str, Any] | None = None, **kw) -> Any:
    is_error, text = await call(w, name, args, **kw)
    assert not is_error, text
    return json.loads(text)


async def fails(w: World, name: str, args: dict[str, Any] | None = None, **kw) -> str:
    is_error, text = await call(w, name, args, **kw)
    assert is_error, text
    return text


# ------------------------------------------------------------------------------ shape


async def test_the_ten_tools_are_listed_and_all_declare_themselves_read_only(world):
    async with Client(build_mcp_server(world.backends)) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False


async def test_no_tool_takes_a_tenant_argument(world):
    async with Client(build_mcp_server(world.backends)) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        properties = set((tool.input_schema or {}).get("properties", {}))
        assert not {p for p in properties if "tenant" in p.lower()}, tool.name


async def test_without_a_signed_in_user_every_tool_refuses(world):
    current_user.set(None)
    calls = [
        ("get_incident", {"incident_id": str(INCIDENT_ID)}),
        ("search_logs", {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}),
        ("search_runbooks", {"query": "pool"}),
        ("get_investigation", {"investigation_id": str(uuid4())}),
        ("read_file", {"service_name": "payment-service", "path": "README.md"}),
    ]
    for name, args in calls:
        assert "Not authenticated" in await fails(world, name, args)
    assert world.api.calls == [] and world.logs.calls == [] and world.runbooks.calls == []


# ------------------------------------------------------------------------ incident tools


async def test_get_incident_returns_the_incident_for_the_callers_tenant(world):
    incident = await ok(world, "get_incident", {"incident_id": str(INCIDENT_ID)})
    assert incident["service_name"] == "payment-service" and incident["occurrence_count"] == 1500
    assert world.api.calls == [(INCIDENT_ID, TENANT)]


async def test_another_tenants_incident_is_simply_not_found(world):
    world.api.payload = incident_payload(tenantId=str(OTHER_TENANT))  # a misbehaving upstream
    assert "Incident not found" in await fails(world, "get_incident", {"incident_id": str(INCIDENT_ID)})


async def test_a_bad_id_gets_a_helpful_message_not_a_stack_trace(world):
    message = await fails(world, "get_incident", {"incident_id": "inc_4821"})
    assert "must be a UUID" in message and "Traceback" not in message


async def test_error_timeline_and_cooccurring_errors_use_the_incidents_own_fingerprint_and_window(world):
    timeline = await ok(world, "get_error_timeline", {"incident_id": str(INCIDENT_ID)})
    assert timeline["total"] == 40 and timeline["bucket"] in {"1m", "5m", "15m", "1h"}
    errors = (await ok(world, "get_cooccurring_errors", {"incident_id": str(INCIDENT_ID)}))["errors"]
    assert errors[0]["service_name"] == "payment-gateway-client"
    assert world.logs.tenants_used() == {TENANT}
    cooccurring = next(p for k, _, p in world.logs.calls if k == "cooccurring")
    assert cooccurring["exclude_fingerprint"] == "f" * 64


async def test_find_related_incidents(world):
    related = (await ok(world, "find_related_incidents", {"incident_id": str(INCIDENT_ID)}))["incidents"]
    assert related[0]["same_fingerprint"] is True and related[0]["service_name"] == "payment-service"
    assert world.history.calls[0]["tenant_id"] == TENANT


# ---------------------------------------------------------------------------- logs


async def test_search_logs_returns_lines_and_scopes_to_the_tenant(world):
    result = await ok(
        world, "search_logs", {"start": "2026-01-01T12:00:00Z", "end": "2026-01-01T13:00:00+00:00", "query": "timeout", "level": "error"}
    )
    assert result["lines"][0]["message"].startswith("Connection timeout")
    kind, tenant, parameters = world.logs.calls[-1]
    assert (kind, tenant) == ("search", TENANT) and parameters["query"] == "timeout" and parameters["level"] == "ERROR"


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"start": "yesterday", "end": "2026-01-01T13:00:00Z"}, "start must be an ISO 8601 time"),
        ({"start": "2026-01-01T12:00:00Z", "end": "soon"}, "end must be an ISO 8601 time"),
        ({"start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z"}, ""),  # end before start
        ({"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"}, ""),  # longer than 7 days
        ({"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z", "level": "LOUD"}, "level must be one of"),
        ({"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z", "limit": 100000}, "limit must be between"),
    ],
)
async def test_search_logs_refuses_bad_input_with_a_clear_message(world, args, fragment):
    message = await fails(world, "search_logs", args)
    assert fragment in message
    assert world.logs.calls == []  # refused before the log store was asked


async def test_a_time_without_a_zone_is_taken_as_utc(world):
    await ok(world, "search_logs", {"start": "2026-01-01T12:00:00", "end": "2026-01-01T13:00:00"})
    assert world.logs.calls


# ------------------------------------------------------------------------- runbooks


async def test_search_runbooks_maps_passages_and_never_uses_a_model(world):
    passage = RunbookPassage(
        chunk_id=uuid4(), runbook_id=uuid4(), title="Gateway playbook", heading_path="Gateway > Timeouts", ordinal=0,
        content="Check the status page.", distance=0.2345,
    )
    world.runbooks.result = RunbookSearchResult(outcome="found", passages=[passage])
    result = await ok(world, "search_runbooks", {"query": "  gateway timeout  "})
    assert result == {
        "outcome": "found",
        "passages": [{"runbook": "Gateway playbook", "section": "Gateway > Timeouts", "content": "Check the status page.", "distance": 0.234}],
    }
    call_ = world.runbooks.calls[0]
    assert call_["tenant_id"] == TENANT and call_["query"] == "gateway timeout" and call_["models"] is None


async def test_search_runbooks_needs_a_query_and_a_configured_search(world):
    assert "must not be empty" in await fails(world, "search_runbooks", {"query": "   "})
    bare = McpBackends(logs=world.logs, incidents=world.api, history=world.history, session_factory=world.backends.session_factory)
    assert "not configured" in await fails(world, "search_runbooks", {"query": "x"}, backends=bare)


# ---------------------------------------------------------------------- investigations


@requires_postgres
async def test_get_investigation_returns_only_the_callers_tenants(world):
    wipe_investigations()
    try:
        inv, _ = make_investigation(TENANT)
        found = await ok(world, "get_investigation", {"investigation_id": str(inv)})
        assert found["status"] == "COMPLETED" and found["rootCause"]["statement"].startswith("The connection pool")
        other, _ = make_investigation(OTHER_TENANT)
        assert "Investigation not found" in await fails(world, "get_investigation", {"investigation_id": str(other)})
        assert "must be a UUID" in await fails(world, "get_investigation", {"investigation_id": "latest"})
    finally:
        wipe_investigations()


# ---------------------------------------------------------------------------- code


async def test_code_tools_read_the_mapped_repository(world):
    found = await ok(world, "search_code", {"service_name": "payment-service", "pattern": "gateway.call"})
    assert found["repository"] == "acme/shop" and len(found["commit"]) == 40 and found["hits"]
    assert world.code.calls == [(TENANT, "payment-service")]
    commits = await ok(world, "recent_commits", {"service_name": "payment-service"})
    assert "gateway timeout" in json.dumps(commits)
    from tests.test_code_investigator import PAYMENT

    file = await ok(world, "read_file", {"service_name": "payment-service", "path": PAYMENT, "start_line": 1, "end_line": 5})
    assert file["repository"] == "acme/shop" and "line 1" in file["text"]


async def test_code_tools_refuse_secret_files_and_paths_that_climb_out(world):
    assert "secrets" in await fails(world, "read_file", {"service_name": "payment-service", "path": ".env"})
    assert await fails(world, "read_file", {"service_name": "payment-service", "path": "../../etc/passwd"})
    assert await fails(world, "search_code", {"service_name": "payment-service", "pattern": "x", "glob": "../*"})


async def test_the_answers_never_contain_the_servers_own_paths(world, repo):
    answer = await ok(world, "search_code", {"service_name": "payment-service", "pattern": "gateway.call"})
    assert str(Path(repo.root)) not in json.dumps(answer)


async def test_code_tools_when_no_repository_is_mapped_or_code_is_not_configured(world, repo):
    world.code.result = None
    assert "No repository is connected" in await fails(world, "search_code", {"service_name": "ghost", "pattern": "x"})
    bare = McpBackends(logs=world.logs, incidents=world.api, history=world.history, session_factory=world.backends.session_factory)
    assert "not configured" in await fails(world, "recent_commits", {"service_name": "x"}, backends=bare)


async def test_a_repository_that_cannot_be_opened_explains_why_without_secrets(world):
    world.code.result = CodeAccessError("The repository could not be fetched (the token was rejected)")
    message = await fails(world, "read_file", {"service_name": "payment-service", "path": "README.md"})
    assert "token was rejected" in message and "ghp_" not in message


# --------------------------------------------------------------------------- failures


async def test_a_log_store_failure_shows_a_safe_message_not_the_internal_one(world):
    world.logs.errors["timeline"] = ClickHouseError("DB::Exception: password for ai_reader is hunter2 at 10.0.0.5")
    message = await fails(world, "get_error_timeline", {"incident_id": str(INCIDENT_ID)})
    assert "log store is not available" in message
    assert "hunter2" not in message and "10.0.0.5" not in message


async def test_an_unexpected_bug_shows_a_generic_message(world):
    world.history.error = RuntimeError("secret internal detail /srv/app/db.py line 12")
    message = await fails(world, "find_related_incidents", {"incident_id": str(INCIDENT_ID)})
    assert "failed unexpectedly" in message and "secret internal detail" not in message and "/srv/app" not in message


async def test_an_incident_service_outage_is_reported_without_internals(world):
    world.api.payload = OSError("connect to 10.1.2.3:8083 refused")
    message = await fails(world, "get_incident", {"incident_id": str(INCIDENT_ID)})
    assert "10.1.2.3" not in message
