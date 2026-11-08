"""Unit tests for the load_context node and the two log tools it uses (no network)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from langgraph.graph import END, START, StateGraph

from app.graph.nodes.load_context import (
    MAX_CONTENT,
    investigation_window,
    load_context,
)
from app.graph.state import (
    InvestigationContext,
    InvestigationState,
    add_evidence,
    new_state,
)
from app.integrations.clickhouse import ClickHouseError
from app.integrations.incident_service import IncidentNotFoundError
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.tools.incidents import IncidentContext
from app.tools.logs import (
    MAX_STACK_TRACE_CHARS,
    MAX_WINDOW,
    get_log_stats,
    get_sample_stack_trace,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")
INCIDENT_ID = UUID("c9d8e7f6-a5b4-4c3d-8e1f-0a9b8c7d6e5f")
FIRST = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LAST = FIRST + timedelta(minutes=10)

JAVA_TRACE = (
    "java.net.SocketTimeoutException: Connection timeout after 30000ms calling payment gateway\n"
    "\tat com.log0.payment.PaymentProcessor.charge(PaymentProcessor.java:120)\n"
    "\tat com.log0.common.Dispatcher.run(Dispatcher.java:88)\n"
)
LIBRARY_ONLY_TRACE = "\tat java.base/java.lang.Thread.run(Thread.java:833)"


def _payload(**over: Any) -> dict[str, Any]:
    data = {
        "incidentId": str(INCIDENT_ID),
        "tenantId": str(TENANT),
        "fingerprint": "f" * 64,
        "serviceName": "payment-service",
        "environment": "production",
        "severity": "HIGH",
        "status": "NEW",
        "occurrenceCount": 1500,
        "firstSeenAt": FIRST.isoformat(),
        "lastSeenAt": LAST.isoformat(),
        "resolvedAt": None,
        "topMessages": ["Connection timeout after 30000ms"],
        "aiSummary": "Possible gateway overload",
    }
    data.update(over)
    return data


class FakeApi:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self.payload = payload

    async def get_incident(self, incident_id: UUID, tenant_id: UUID) -> dict[str, Any]:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeLogs:
    """Answers the two ClickHouse queries load_context makes; records how it was called."""

    def __init__(
        self,
        stats_rows: list[dict[str, Any]] | None = None,
        trace: str | None = None,
        *,
        stats_error: Exception | None = None,
        trace_error: Exception | None = None,
    ) -> None:
        self.stats_rows = stats_rows if stats_rows is not None else _stats_rows()
        self.trace = trace
        self.stats_error = stats_error
        self.trace_error = trace_error
        self.calls: list[tuple[str, UUID, dict[str, Any]]] = []

    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None):
        self.calls.append((sql, tenant_id, parameters or {}))
        if "stack_trace" in sql:
            if self.trace_error:
                raise self.trace_error
            return [{"stack_trace": self.trace, "n": 3}] if self.trace else []
        if self.stats_error:
            raise self.stats_error
        return self.stats_rows


def _stats_rows() -> list[dict[str, Any]]:
    return [
        {"service_name": "payment-service", "level": "ERROR", "n": 40, "first_ts": FIRST, "last_ts": LAST},
        {"service_name": "payment-service", "level": "WARN", "n": 2, "first_ts": FIRST, "last_ts": LAST},
        {"service_name": "checkout", "level": "ERROR", "n": 5, "first_ts": FIRST + timedelta(minutes=1), "last_ts": LAST},
    ]


def _context(api: FakeApi, logs: FakeLogs) -> InvestigationContext:
    return InvestigationContext(tenant_id=TENANT, logs=logs, incidents=api, history=SimpleNamespace())  # type: ignore[arg-type]


async def _run(api: FakeApi, logs: FakeLogs) -> InvestigationState:
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("load_context", load_context)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", END)
    start = new_state(investigation_id="i1", incident_id=str(INCIDENT_ID), scope=InvestigationScope())
    return await builder.compile().ainvoke(start, context=_context(api, logs))


# ----------------------------------------------------------------- the happy path


@pytest.mark.asyncio
async def test_load_context_fills_incident_frames_evidence_and_counts_tool_calls() -> None:
    logs = FakeLogs(trace=JAVA_TRACE)
    final = await _run(FakeApi(_payload()), logs)

    assert final["incident"].service_name == "payment-service"
    assert [f.function for f in final["stack_frames"]] == ["charge", "run"]
    assert final["stack_frames"][0].path_hint == "com/log0/payment/PaymentProcessor.java"
    assert final["gaps"] == [] and final["tool_calls"] == 3
    assert final["status"] == InvestigationStatus.RUNNING and final["error"] is None

    kinds = sorted(e.kind for e in final["evidence"])
    assert kinds == ["incident", "log", "log"]  # incident, log statistics, stack trace
    assert len({e.id for e in final["evidence"]}) == 3
    by_ref = {e.source_ref.split()[0]: e for e in final["evidence"]}
    assert "HIGH incident on payment-service (production)" in by_ref["incident"].summary
    assert "47 log events" in by_ref["logstats"].summary and "ERROR 45, WARN 2" in by_ref["logstats"].summary
    assert "SocketTimeoutException" in by_ref["stacktrace"].summary
    assert "PaymentProcessor.java:120 in charge" in by_ref["stacktrace"].summary
    assert by_ref["stacktrace"].content == JAVA_TRACE


@pytest.mark.asyncio
async def test_every_query_uses_the_tenant_from_the_context_and_the_incident_window() -> None:
    logs = FakeLogs(trace=JAVA_TRACE)
    await _run(FakeApi(_payload()), logs)
    assert len(logs.calls) == 2
    for _sql, tenant, params in logs.calls:
        assert tenant == TENANT
        assert params["fingerprint"] == "f" * 64
        assert params["start"] == FIRST - timedelta(minutes=30)
        assert params["end"] == LAST + timedelta(minutes=1)


def test_a_long_running_incident_is_clamped_to_the_most_recent_week() -> None:
    incident = IncidentContext(
        incident_id=INCIDENT_ID, fingerprint="f", service_name="s", environment="e", severity="HIGH",
        status="NEW", occurrence_count=1, first_seen_at=LAST - timedelta(days=30), last_seen_at=LAST,
        resolved_at=None, top_messages=[], ai_summary=None,
    )
    start, end = investigation_window(incident)
    assert end == LAST + timedelta(minutes=1)  # the latest events are always kept
    assert end - start == MAX_WINDOW


@pytest.mark.asyncio
async def test_loading_twice_gives_the_same_evidence_ids_so_nothing_is_duplicated() -> None:
    first = await _run(FakeApi(_payload()), FakeLogs(trace=JAVA_TRACE))
    second = await _run(FakeApi(_payload()), FakeLogs(trace=JAVA_TRACE))
    assert [e.id for e in first["evidence"]] == [e.id for e in second["evidence"]]
    assert len(add_evidence(first["evidence"], second["evidence"])) == 3


@pytest.mark.asyncio
async def test_evidence_content_is_capped() -> None:
    huge = _payload(aiSummary="x" * 20_000, topMessages=["m" * 3000] * 5)
    final = await _run(FakeApi(huge), FakeLogs(trace="java.lang.Error: boom\n\tat com.log0.A.m(A.java:1)\n" + "x" * 20_000))
    assert all(len(e.content) <= MAX_CONTENT for e in final["evidence"])


# ------------------------------------------------- missing data becomes gaps, not failures


@pytest.mark.asyncio
async def test_no_stack_trace_is_a_gap() -> None:
    final = await _run(FakeApi(_payload()), FakeLogs(trace=None))
    assert final["stack_frames"] == []
    assert final["gaps"] == ["No stack trace was recorded for this incident"]
    assert sorted(e.kind for e in final["evidence"]) == ["incident", "log"]  # no stack trace evidence


@pytest.mark.asyncio
async def test_library_only_trace_is_kept_but_flagged() -> None:
    final = await _run(FakeApi(_payload()), FakeLogs(trace=LIBRARY_ONLY_TRACE))
    assert len(final["stack_frames"]) == 1
    assert final["gaps"] == ["The stack trace contains only library frames"]


@pytest.mark.asyncio
async def test_unparseable_trace_keeps_the_raw_text_as_evidence() -> None:
    final = await _run(FakeApi(_payload()), FakeLogs(trace="something went wrong, no frames here"))
    assert final["stack_frames"] == []
    assert final["gaps"] == ["The stack trace could not be parsed into frames"]
    trace_evidence = next(e for e in final["evidence"] if e.source_ref.startswith("stacktrace"))
    assert "unrecognised format" in trace_evidence.summary and "something went wrong" in trace_evidence.content


@pytest.mark.asyncio
async def test_no_log_events_is_a_gap() -> None:
    final = await _run(FakeApi(_payload()), FakeLogs(stats_rows=[], trace=JAVA_TRACE))
    assert final["gaps"] == ["No log events were found for this incident's fingerprint in the investigation window"]
    assert not any(e.source_ref.startswith("logstats") for e in final["evidence"])


@pytest.mark.asyncio
async def test_clickhouse_outage_degrades_to_gaps_and_keeps_the_incident() -> None:
    down = ClickHouseError("connection refused")
    final = await _run(FakeApi(_payload()), FakeLogs(stats_error=down, trace_error=down))
    assert final["incident"] is not None and final["status"] == InvestigationStatus.RUNNING
    assert len(final["gaps"]) == 2 and all("could not be queried" in g for g in final["gaps"])
    assert [e.kind for e in final["evidence"]] == ["incident"]
    assert final["tool_calls"] == 3  # the attempts still count against the budget


@pytest.mark.asyncio
async def test_one_failing_query_does_not_hide_the_other() -> None:
    final = await _run(FakeApi(_payload()), FakeLogs(stats_error=ClickHouseError("x"), trace=JAVA_TRACE))
    assert [f.function for f in final["stack_frames"]] == ["charge", "run"]  # the trace still arrived
    assert final["gaps"] == ["Log statistics are unavailable (the log store could not be queried)"]


@pytest.mark.asyncio
async def test_an_unexpected_error_is_not_swallowed() -> None:
    with pytest.raises(ZeroDivisionError):
        await _run(FakeApi(_payload()), FakeLogs(stats_error=ZeroDivisionError("bug"), trace=JAVA_TRACE))


# ------------------------------------------------------------- the incident is required


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api", "error"),
    [
        (FakeApi(IncidentNotFoundError("nope")), "Incident not found"),
        (FakeApi(_payload(tenantId=str(OTHER))), "Incident not found"),  # remote ignored our tenant filter
        (FakeApi(httpx.ConnectError("refused")), "Incident service is unavailable"),
    ],
)
async def test_without_the_incident_the_investigation_fails_cleanly(api: FakeApi, error: str) -> None:
    logs = FakeLogs(trace=JAVA_TRACE)
    final = await _run(api, logs)
    assert final["status"] == InvestigationStatus.FAILED and final["error"] == error
    assert final["incident"] is None and final["evidence"] == []
    assert logs.calls == [] and final["tool_calls"] == 1  # nothing else was attempted


# ------------------------------------------------------------- the two new log tools

WINDOW_START = FIRST
WINDOW_END = FIRST + timedelta(hours=1)


@pytest.mark.asyncio
async def test_log_stats_aggregates_services_and_levels() -> None:
    stats = await get_log_stats(FakeLogs(), tenant_id=TENANT, fingerprint="fp", start=WINDOW_START, end=WINDOW_END)
    assert stats.total == 47
    assert [(s.service_name, s.count) for s in stats.services] == [("payment-service", 42), ("checkout", 5)]
    assert stats.levels == {"ERROR": 45, "WARN": 2}
    assert stats.first_seen == FIRST and stats.last_seen == LAST


@pytest.mark.asyncio
async def test_log_stats_with_no_rows_is_empty_not_an_error() -> None:
    stats = await get_log_stats(FakeLogs(stats_rows=[]), tenant_id=TENANT, fingerprint="fp", start=WINDOW_START, end=WINDOW_END)
    assert stats.total == 0 and stats.first_seen is None and stats.services == [] and stats.levels == {}


@pytest.mark.asyncio
async def test_sample_stack_trace_returns_the_top_row_truncated_and_binds_values() -> None:
    logs = FakeLogs(trace="t" * (MAX_STACK_TRACE_CHARS + 500))
    trace = await get_sample_stack_trace(logs, tenant_id=TENANT, fingerprint="x' OR 1=1 --", start=WINDOW_START, end=WINDOW_END)
    assert trace is not None and len(trace) == MAX_STACK_TRACE_CHARS
    sql, _, params = logs.calls[0]
    assert "trace_id AS stack_trace" in sql  # the one place that knows the column's real meaning
    assert "x' OR 1=1" not in sql and params["fingerprint"] == "x' OR 1=1 --"
    assert await get_sample_stack_trace(FakeLogs(), tenant_id=TENANT, fingerprint="fp", start=WINDOW_START, end=WINDOW_END) is None


@pytest.mark.asyncio
async def test_new_log_tools_reject_windows_over_seven_days() -> None:
    with pytest.raises(ValueError, match="at most"):
        await get_log_stats(FakeLogs(), tenant_id=TENANT, fingerprint="fp", start=WINDOW_START, end=WINDOW_START + timedelta(days=8))
    with pytest.raises(ValueError, match="end must be after"):
        await get_sample_stack_trace(FakeLogs(), tenant_id=TENANT, fingerprint="fp", start=WINDOW_END, end=WINDOW_START)
