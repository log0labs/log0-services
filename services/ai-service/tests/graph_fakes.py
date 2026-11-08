"""Shared fakes for graph tests: log store, incident API, incident history and chat model.

Nothing here touches the network. ``FakeLogs`` recognises each of log0's five ClickHouse
queries by a marker in its SQL, so one fake serves ``load_context`` and every gather node.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableLambda

from app.graph.state import InvestigationContext
from app.llm.purpose import Purpose
from app.schemas.domain import Triage

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")
INCIDENT_ID = UUID("c9d8e7f6-a5b4-4c3d-8e1f-0a9b8c7d6e5f")
FIRST = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LAST = FIRST + timedelta(minutes=10)
FINGERPRINT = "f" * 64

JAVA_TRACE = (
    "java.net.SocketTimeoutException: Connection timeout after 30000ms calling payment gateway\n"
    "\tat com.log0.payment.PaymentProcessor.charge(PaymentProcessor.java:120)\n"
    "\tat com.log0.common.Dispatcher.run(Dispatcher.java:88)\n"
)


def incident_payload(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "incidentId": str(INCIDENT_ID),
        "tenantId": str(TENANT),
        "fingerprint": FINGERPRINT,
        "serviceName": "payment-service",
        "environment": "production",
        "severity": "HIGH",
        "status": "NEW",
        "occurrenceCount": 1500,
        "firstSeenAt": FIRST.isoformat(),
        "lastSeenAt": LAST.isoformat(),
        "resolvedAt": None,
        "topMessages": ["Connection timeout after 30000ms"],
        "aiSummary": None,
    }
    data.update(over)
    return data


class FakeApi:
    def __init__(self, payload: dict[str, Any] | Exception | None = None) -> None:
        self.payload = incident_payload() if payload is None else payload
        self.calls: list[tuple[UUID, UUID]] = []

    async def get_incident(self, incident_id: UUID, tenant_id: UUID) -> dict[str, Any]:
        self.calls.append((incident_id, tenant_id))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeLogs:
    """Answers all five log queries. ``errors`` maps a query name to an exception to raise:
    one of ``stats``, ``trace``, ``timeline``, ``cooccurring``, ``search``."""

    def __init__(
        self,
        *,
        trace: str | None = JAVA_TRACE,
        timeline_rows: list[dict[str, Any]] | None = None,
        cooccurring_rows: list[dict[str, Any]] | None = None,
        search_rows: list[dict[str, Any]] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.trace = trace
        self.timeline_rows = timeline_rows if timeline_rows is not None else [
            {"bucket_start": FIRST, "n": 10},
            {"bucket_start": FIRST + timedelta(minutes=1), "n": 30},
        ]
        self.cooccurring_rows = cooccurring_rows if cooccurring_rows is not None else [
            {
                "service_name": "payment-gateway-client",
                "fingerprint": "e" * 64,
                "message_template": "upstream returned 503",
                "n": 12,
                "first_ts": FIRST,
                "last_ts": LAST,
            }
        ]
        self.search_rows = search_rows if search_rows is not None else [
            {
                "timestamp": LAST,
                "service_name": "payment-service",
                "level": "ERROR",
                "message": "Connection timeout after 30000ms",
                "fingerprint": FINGERPRINT,
            }
        ]
        self.errors = errors or {}
        self.calls: list[tuple[str, UUID, dict[str, Any]]] = []

    @staticmethod
    def kind(sql: str) -> str:
        if "stack_trace" in sql:
            return "trace"
        if "toStartOfInterval" in sql:
            return "timeline"
        if "GROUP BY service_name, fingerprint" in sql:
            return "cooccurring"
        if "ORDER BY timestamp DESC" in sql:
            return "search"
        return "stats"

    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None):
        kind = self.kind(sql)
        self.calls.append((kind, tenant_id, parameters or {}))
        if kind in self.errors:
            raise self.errors[kind]
        if kind == "trace":
            return [{"stack_trace": self.trace, "n": 3}] if self.trace else []
        if kind == "timeline":
            return self.timeline_rows
        if kind == "cooccurring":
            return self.cooccurring_rows
        if kind == "search":
            return self.search_rows
        return [
            {"service_name": "payment-service", "level": "ERROR", "n": 40, "first_ts": FIRST, "last_ts": LAST}
        ]

    def tenants_used(self) -> set[UUID]:
        return {tenant for _, tenant, _ in self.calls}


def history_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "incident_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "fingerprint": FINGERPRINT,
        "service_name": "payment-service",
        "environment": "production",
        "severity": "HIGH",
        "status": "RESOLVED",
        "occurrence_count": 120,
        "first_seen_at": datetime(2025, 12, 20, 9, 0, tzinfo=UTC),
        "last_seen_at": datetime(2025, 12, 20, 10, 0, tzinfo=UTC),
        "resolved_at": datetime(2025, 12, 20, 11, 0, tzinfo=UTC),
        "ai_summary": "Gateway timeout raised from 5s to 30s after an upstream slowdown",
        "same_fingerprint": True,
    }
    row.update(over)
    return row


class FakeHistory:
    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.rows = rows if rows is not None else [history_row()]
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def find_related(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.rows


class FakeModel:
    """A chat model whose structured output is canned. Records the messages it was sent."""

    def __init__(self, answer: Any = None, *, error: Exception | None = None, delay: float = 0) -> None:
        self.answer = answer
        self.error = error
        self.delay = delay
        self.messages: list[Any] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> RunnableLambda:
        async def call(messages: Any) -> Any:
            self.messages.append(messages)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            return self.answer

        return RunnableLambda(lambda m: None, afunc=call)


class FakeModels:
    def __init__(self, model: FakeModel) -> None:
        self.model = model
        self.purposes: list[Purpose] = []

    def for_purpose(self, purpose: Purpose) -> FakeModel:
        self.purposes.append(purpose)
        return self.model


def triage_answer(**over: Any) -> Triage:
    data: dict[str, Any] = {
        "category": "dependency",
        "summary": "The payment gateway is timing out.",
        "gather": ["logs", "history"],
        "initial_hypotheses": ["Gateway is overloaded"],
    }
    data.update(over)
    return Triage(**data)


def make_context(
    *,
    api: FakeApi | None = None,
    logs: FakeLogs | None = None,
    history: FakeHistory | None = None,
    models: FakeModels | None = None,
    tenant: UUID = TENANT,
) -> InvestigationContext:
    return InvestigationContext(
        tenant_id=tenant,
        logs=logs or FakeLogs(),
        incidents=api or FakeApi(),
        history=history or FakeHistory(),
        models=models or FakeModels(FakeModel(triage_answer())),
    )


def unused(name: str = "unused") -> SimpleNamespace:
    return SimpleNamespace(name=name)
