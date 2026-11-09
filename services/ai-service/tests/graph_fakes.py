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

from app.graph.budget import Budget
from app.graph.state import InvestigationContext
from app.llm.purpose import Purpose, StructuredOutputMethod
from app.schemas.domain import (
    HypothesisDraft,
    HypothesisVerdict,
    Synthesis,
    Triage,
    Verification,
    make_evidence_id,
)

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
    """A chat model whose structured output is canned. Records the messages it was sent.

    ``answer`` is what it returns for any schema; ``answers`` overrides that per schema, so one
    model can play both synthesize (``Synthesis``) and verify (``Verification``). Asked for a
    ``Verification`` with no answer for it, it gives no verdicts: hypotheses stay open.
    """

    def __init__(
        self,
        answer: Any = None,
        *,
        answers: dict[Any, Any] | None = None,
        error: Exception | None = None,
        delay: float = 0,
    ) -> None:
        self.answer = answer
        self.answers = answers or {}  # a list is played in order, the last one repeating
        self.calls = 0
        self.error = error
        self.delay = delay
        self.messages: list[Any] = []
        self.options: dict[str, Any] = {}

    def with_structured_output(self, schema: Any, **kwargs: Any) -> RunnableLambda:
        self.options = kwargs  # e.g. {"method": "json_schema"}

        async def call(messages: Any) -> Any:
            self.messages.append(messages)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            if schema in self.answers:
                value = self.answers[schema]
                if isinstance(value, list):
                    value = value[min(self.calls, len(value) - 1)]
                self.calls += 1
                return value
            if schema is Verification:
                return Verification(verdicts=[])  # nothing configured: no opinion on any hypothesis
            return self.answer

        return RunnableLambda(lambda m: None, afunc=call)


class FakeModels:
    """Hands out ``model`` for triage and ``investigation`` (a valid synthesis by default) for
    synthesize, and records which purposes were asked for."""

    def __init__(self, model: FakeModel, *, investigation: FakeModel | None = None) -> None:
        self.model = model
        self.investigation = investigation or FakeModel(synthesis_answer())
        self.purposes: list[Purpose] = []

    def for_purpose(self, purpose: Purpose) -> FakeModel:
        self.purposes.append(purpose)
        return self.investigation if purpose == Purpose.INVESTIGATION else self.model

    def structured_method(self, purpose: Purpose) -> StructuredOutputMethod:
        return "function_calling" if purpose == Purpose.INVESTIGATION else "json_schema"  # the settings defaults


def triage_answer(**over: Any) -> Triage:
    data: dict[str, Any] = {
        "category": "dependency",
        "summary": "The payment gateway is timing out.",
        "gather": ["logs", "history"],
        "initial_hypotheses": ["Gateway is overloaded"],
    }
    data.update(over)
    return Triage(**data)


# The ids load_context gives the incident and the stack trace (content hashes, so computable here)
INCIDENT_EVIDENCE_ID = make_evidence_id("incident", f"incident {INCIDENT_ID}")
STACKTRACE_EVIDENCE_ID = make_evidence_id("log", f"stacktrace {FINGERPRINT[:12]}")


def synthesis_answer(**over: Any) -> Synthesis:
    drafts = over.pop("hypotheses", None)
    if drafts is None:
        drafts = [
            HypothesisDraft(
                statement="The payment gateway is slow, so calls exceed the 30 s timeout.",
                supporting_evidence=[STACKTRACE_EVIDENCE_ID, INCIDENT_EVIDENCE_ID],
            ),
            HypothesisDraft(
                statement="The connection pool is exhausted, so requests queue until they time out.",
                supporting_evidence=[INCIDENT_EVIDENCE_ID],
                contradicting_evidence=[STACKTRACE_EVIDENCE_ID],
            ),
        ]
    return Synthesis(hypotheses=drafts, **over)


def verdict(hypothesis_id: str, kind: str = "open", **over: Any) -> HypothesisVerdict:
    return HypothesisVerdict(hypothesis_id=hypothesis_id, verdict=kind, **over)  # type: ignore[arg-type]


def verification_answer(*verdicts: HypothesisVerdict) -> Verification:
    return Verification(verdicts=list(verdicts))


def make_context(
    *,
    api: FakeApi | None = None,
    logs: FakeLogs | None = None,
    history: FakeHistory | None = None,
    models: FakeModels | None = None,
    tenant: UUID = TENANT,
    budget: Budget | None = None,
) -> InvestigationContext:
    return InvestigationContext(
        tenant_id=tenant,
        logs=logs or FakeLogs(),
        incidents=api or FakeApi(),
        history=history or FakeHistory(),
        models=models or FakeModels(FakeModel(triage_answer())),
        budget=budget or Budget(),
    )


def unused(name: str = "unused") -> SimpleNamespace:
    return SimpleNamespace(name=name)
