"""load_context against the real ClickHouse (needs ai_reader and CLICKHOUSE_PASSWORD)."""

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import clickhouse_connect
import pytest
import pytest_asyncio
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graph.nodes.load_context import load_context
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.integrations.clickhouse import LogStore
from app.schemas.investigation import InvestigationScope
from app.tools.logs import get_log_stats, get_sample_stack_trace

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="ClickHouse integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FINGERPRINT = "fp-load-context-test"
TRACE = (
    "java.net.SocketTimeoutException: Connection timeout calling gateway\n"
    "\tat com.log0.payment.PaymentProcessor.charge(PaymentProcessor.java:120)\n"
    "\tat com.log0.common.Dispatcher.run(Dispatcher.java:88)"
)
COLUMNS = [
    "event_id", "tenant_id", "service_name", "environment", "timestamp",
    "level", "message", "message_template", "fingerprint", "trace_id", "schema_version",
]


def _row(tenant: UUID, minute: int, service: str, level: str, trace: str = "") -> list:
    return [
        str(uuid4()), str(tenant), service, "prod", T0 + timedelta(minutes=minute),
        level, "Connection timeout", "Connection timeout", FINGERPRINT, trace, "1",
    ]


@pytest_asyncio.fixture
async def tenants() -> AsyncGenerator[tuple[UUID, UUID]]:
    """Tenant A: 4 ERROR (3 with a trace) in payment + 1 WARN in checkout. Tenant B: same fingerprint."""
    a, b = uuid4(), uuid4()
    rows = [
        _row(a, 1, "payment", "ERROR", TRACE),
        _row(a, 2, "payment", "ERROR", TRACE),
        _row(a, 3, "payment", "ERROR", TRACE),
        _row(a, 4, "payment", "ERROR"),
        _row(a, 5, "checkout", "WARN"),
        _row(b, 1, "payment", "ERROR", "java.lang.Error: OTHER TENANT\n\tat com.other.Secret.run(Secret.java:1)"),
    ]
    admin = await clickhouse_connect.get_async_client(
        host="localhost", port=8123, username="log0", password="log0", database="log0"
    )
    await admin.insert("log_events", rows, column_names=COLUMNS)
    try:
        yield a, b
    finally:
        await admin.command(
            "ALTER TABLE log_events DELETE WHERE tenant_id IN ({a:String}, {b:String})",
            parameters={"a": str(a), "b": str(b)},
            settings={"mutations_sync": 1},
        )
        await admin.close()


@pytest_asyncio.fixture
async def store() -> AsyncGenerator[LogStore]:
    s = LogStore(get_settings())
    yield s
    await s.close()


class FakeApi:
    def __init__(self, tenant: UUID) -> None:
        self.tenant = tenant

    async def get_incident(self, incident_id: UUID, tenant_id: UUID) -> dict:
        return {
            "incidentId": str(incident_id), "tenantId": str(tenant_id), "fingerprint": FINGERPRINT,
            "serviceName": "payment", "environment": "prod", "severity": "HIGH", "status": "NEW",
            "occurrenceCount": 5, "firstSeenAt": (T0 + timedelta(minutes=1)).isoformat(),
            "lastSeenAt": (T0 + timedelta(minutes=5)).isoformat(), "resolvedAt": None,
            "topMessages": ["Connection timeout"], "aiSummary": None,
        }


async def _run(store: LogStore, tenant: UUID) -> InvestigationState:
    ctx = InvestigationContext(tenant_id=tenant, logs=store, incidents=FakeApi(tenant), history=SimpleNamespace())  # type: ignore[arg-type]
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("load_context", load_context)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", END)
    state = new_state(investigation_id="i", incident_id=str(uuid4()), scope=InvestigationScope())
    return await builder.compile().ainvoke(state, context=ctx)


async def test_load_context_reads_real_logs_and_parses_the_real_trace(store: LogStore, tenants) -> None:
    a, _ = tenants
    final = await _run(store, a)
    assert final["gaps"] == []
    assert [(f.function, f.line) for f in final["stack_frames"]] == [("charge", 120), ("run", 88)]
    stats = next(e for e in final["evidence"] if e.source_ref.startswith("logstats"))
    assert "5 log events" in stats.summary and "ERROR 4, WARN 1" in stats.summary
    assert "payment: 4" in stats.content and "checkout: 1" in stats.content
    trace = next(e for e in final["evidence"] if e.source_ref.startswith("stacktrace"))
    assert "PaymentProcessor.java:120 in charge" in trace.summary


async def test_another_tenants_trace_never_appears(store: LogStore, tenants) -> None:
    a, b = tenants
    for_a = await _run(store, a)
    for_b = await _run(store, b)
    assert all("OTHER TENANT" not in e.content for e in for_a["evidence"])
    assert [f.function for f in for_b["stack_frames"]] == ["run"]  # B sees only its own trace
    assert "1 log events" in next(e for e in for_b["evidence"] if e.source_ref.startswith("logstats")).summary


async def test_log_tools_against_real_data(store: LogStore, tenants) -> None:
    a, _ = tenants
    window = {"start": T0, "end": T0 + timedelta(hours=1)}
    stats = await get_log_stats(store, tenant_id=a, fingerprint=FINGERPRINT, **window)
    assert stats.total == 5 and stats.first_seen == T0 + timedelta(minutes=1)
    assert [(s.service_name, s.count) for s in stats.services] == [("payment", 4), ("checkout", 1)]
    assert await get_sample_stack_trace(store, tenant_id=a, fingerprint=FINGERPRINT, **window) == TRACE
    assert await get_sample_stack_trace(store, tenant_id=uuid4(), fingerprint=FINGERPRINT, **window) is None
    assert (await get_log_stats(store, tenant_id=uuid4(), fingerprint=FINGERPRINT, **window)).total == 0
