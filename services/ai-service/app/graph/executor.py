"""Run one investigation through the graph: the real ``execute`` for the runner.

The runner owns the lifecycle (concurrency cap, QUEUED -> RUNNING, cancel, final
COMPLETED). This class owns the work in the middle: build the starting state from the
investigation row, run (or resume) the graph, write each step as an event, and record what
the run cost. Its only decision about status is the one the graph made: if a node ended the
run as FAILED (for example the incident could not be read), the investigation is FAILED.
"""

import logging
from typing import Any
from uuid import UUID

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.config import Settings
from app.graph.builder import GRAPH_VERSION
from app.graph.code_access import CodeAccess
from app.graph.events import events_from_update
from app.graph.gates import Gate, gate_for
from app.graph.result import result_for_row
from app.graph.state import ChatModels, InvestigationContext, new_state
from app.graph.usage import Prices, UsageRecorder, run_config
from app.llm.model_spec import ModelSpec
from app.llm.models import PurposeChatModels
from app.llm.purpose import Purpose
from app.llm.resolver import PlatformLlmResolver, TenantLlmResolver
from app.observability import tracing_allowed, tracing_scope
from app.publishing.ci import CiWait
from app.publishing.policy import PrLimits
from app.publishing.port import Publisher
from app.retrieval.memory import Memory, NewMemory
from app.retrieval.runbooks import RunbookSource
from app.runner import SessionFactory
from app.schemas.investigation import InvestigationStatus
from app.security.crypto import CryptoError, SecretBox, secret_context
from app.security.redaction import redact_text
from app.store import events as event_store
from app.store import investigations as investigation_store
from app.store import provider_config as provider_config_store
from app.tools.incidents import IncidentApi, RelatedIncidentSource
from app.tools.logs import LogBackend

logger = logging.getLogger(__name__)


def pending_interrupts(snapshot: Any) -> list[Any]:
    """The interrupts a paused graph is waiting on (empty when it is not waiting for a human)."""
    return [interrupt for task in snapshot.tasks for interrupt in task.interrupts]



def state_for_row(row: Any) -> Any:
    """The starting state for an investigation row. The question is redacted here, because it is copied into
    prompts and search queries; the stored row keeps exactly what the engineer typed."""
    return new_state(
        investigation_id=str(row.id),
        incident_id=str(row.incident_id),
        scope=row.scope,
        question=redact_text(row.question) if row.question else None,
    )


def usage_for_row(recorder: UsageRecorder) -> dict[str, Any]:
    """Tokens and cost for the row; nothing when no call reported usage (the columns stay empty)."""
    if not recorder.has_usage:
        return {}
    return {
        "input_tokens": recorder.input_tokens,
        "output_tokens": recorder.output_tokens,
        "cost_usd": recorder.cost_usd(),
    }


class GraphExecutor:
    """Holds the compiled graph and the backends; ``run`` matches the runner's ``ExecuteFn``."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        *,
        logs: LogBackend,
        incidents: IncidentApi,
        history: RelatedIncidentSource,
        models: ChatModels,
        prices: Prices | None = None,
        code: CodeAccess | None = None,
        runbooks: RunbookSource | None = None,
        memory: Memory | None = None,
        settings: Settings | None = None,
        require_review: bool = False,
        secret_box: SecretBox | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        self._graph = graph
        self._logs = logs
        self._incidents = incidents
        self._history = history
        self._models = models
        self._prices = prices or {}
        self._code = code
        self._runbooks = runbooks
        self._memory = memory
        self._settings = settings
        self._require_review = require_review
        self._secret_box = secret_box
        self._publisher = publisher

    def _ci_policy(self) -> dict[str, Any]:
        """The CI wait and the only-on-green rule from settings (the defaults when there are none, as in tests)."""
        if self._settings is None:
            return {}
        wait = CiWait(self._settings.ci_wait_seconds, self._settings.ci_poll_seconds, self._settings.ci_start_grace_seconds)
        limits = PrLimits(
            self._settings.pr_max_files,
            self._settings.pr_max_changed_lines,
            self._settings.pr_max_per_repository_per_hour,
            self._settings.pr_max_per_tenant_per_hour,
        )
        return {"ci_wait": wait, "pr_only_on_green": self._settings.pr_only_on_green, "pr_limits": limits}

    async def run(self, session_factory: SessionFactory, investigation_id: UUID, tenant_id: UUID) -> None:
        allowed = True  # without settings (tests) nothing is restricted
        if self._settings is not None:
            async with session_factory() as session:
                byok = await provider_config_store.has_own_key(session, tenant_id=tenant_id)
            allowed = tracing_allowed(self._settings, tenant_id=tenant_id, byok=byok)
            if self._settings.langchain_tracing_v2 and not allowed:
                logger.info("tracing is off for investigation %s: the tenant uses its own LLM key and has not opted in", investigation_id)
        with tracing_scope(allowed):
            await self._run(session_factory, investigation_id, tenant_id)

    async def _models_for(self, session_factory: SessionFactory, tenant_id: UUID) -> ChatModels:
        """The platform models, unless the workspace saved its own choice (model, key, temperature, token limit) in
        Settings, AI. A stored key that cannot be read is skipped with a warning and the platform key is used."""
        if self._settings is None:
            return self._models
        async with session_factory() as session:
            rows = await provider_config_store.list_for_tenant(session, tenant_id=tenant_id)
        if not rows:
            return self._models
        specs: dict[Purpose, ModelSpec] = {}
        keys: dict[str, Any] = {}
        overrides: dict[Purpose, tuple[float | None, int | None]] = {}
        order = {"investigation": 0, "triage": 1, "summary": 2, "evaluation": 3}
        for row in sorted(rows, key=lambda r: order.get(r.purpose, 9)):
            try:
                purpose = Purpose(row.purpose)
            except ValueError:
                continue
            specs[purpose] = ModelSpec(provider=row.provider, model=row.model)
            overrides[purpose] = (row.temperature, row.max_tokens)
            stored = row.stored_key()
            if stored is not None and self._secret_box is not None and row.provider not in keys:
                try:
                    context = secret_context(tenant_id, "provider_config", purpose.value)
                    keys[row.provider] = self._secret_box.decrypt(stored.ciphertext, key_id=stored.key_id, context=context)
                except CryptoError:
                    logger.warning("a stored %s key of tenant %s could not be read; the platform key is used", row.provider, tenant_id)
        resolver = TenantLlmResolver(PlatformLlmResolver(self._settings), specs, keys)
        return PurposeChatModels(self._settings, resolver, overrides=overrides)

    async def _run(self, session_factory: SessionFactory, investigation_id: UUID, tenant_id: UUID) -> None:
        # The row is the authority on who owns this investigation and what was asked.
        async with session_factory() as session:
            row = await investigation_store.get_by_id(
                session,
                tenant_id=tenant_id,
                investigation_id=investigation_id
            )
        if row is None:
            raise LookupError(f"Investigation {investigation_id} not found for tenant {tenant_id}")

        # thread_id = investigation id: one checkpoint thread per investigation, never shared.
        # The recorder listens to every node and model call of this run (tokens, time, cost).
        recorder = UsageRecorder(self._prices)
        config: Any = run_config(investigation_id, recorder, GRAPH_VERSION)
        context = InvestigationContext(
            tenant_id=tenant_id,  # from the investigation row the runner verified, never the model
            logs=self._logs,
            incidents=self._incidents,
            history=self._history,
            models=await self._models_for(session_factory, tenant_id),
            code=self._code,
            runbooks=self._runbooks,
            memory=self._memory,
            require_review=self._require_review,
            publisher=self._publisher,
            **self._ci_policy(),
        )

        snapshot = await self._graph.aget_state(config)
        waiting = pending_interrupts(snapshot)
        if waiting:
            # The graph is paused at a human gate. Resume it only with the decision the API recorded.
            gate = gate_for(waiting[0].value)
            decision = await self._decision_for_gate(gate, session_factory, tenant_id, investigation_id)
            if decision is None:  # resumed without a decision (a resume that did not complete): wait again
                await self._await_gate(gate, session_factory, investigation_id, tenant_id, waiting[0].value, announce=False)
                return
            logger.info("Resuming investigation %s with the %s decision %s", investigation_id, gate.name, decision["action"])
            await self._stream(Command(resume=decision), config, context, session_factory, investigation_id)
        elif snapshot.next:
            # The service stopped mid-run (restart, deploy): continue from the last checkpoint,
            # so finished nodes are not repeated and their tokens are not spent twice.
            logger.info("Resuming investigation %s before %s", investigation_id, snapshot.next)
            await self._stream(None, config, context, session_factory, investigation_id)
        elif not snapshot.values:
            state = state_for_row(row)
            await self._stream(state, config, context, session_factory, investigation_id)
        # else: the graph already finished but the service stopped before the row was updated;
        # nothing to run, only the final bookkeeping below.

        snapshot = await self._graph.aget_state(config)
        final = snapshot.values
        async with session_factory() as session:
            await investigation_store.record_run_result(
                session,
                investigation_id=investigation_id,
                tenant_id=tenant_id,
                graph_version=GRAPH_VERSION,
                tool_call_count=final["tool_calls"],
                **result_for_row(final),
                **usage_for_row(recorder),
            )
        recorder.log_summary(investigation_id)

        waiting = pending_interrupts(snapshot)
        if waiting:  # the run reached a human gate: stop here, nothing is running while it waits
            await self._await_gate(gate_for(waiting[0].value), session_factory, investigation_id, tenant_id, waiting[0].value, announce=True)
            return

        if final["status"] == InvestigationStatus.FAILED:
            error = final["error"] or "Investigation failed"
            async with session_factory() as session:
                await investigation_store.transition_status(
                    session,
                    investigation_id=investigation_id,
                    tenant_id=tenant_id,
                    status=InvestigationStatus.FAILED,
                    error=error,
                    set_completed=True,
                )
            await event_store.append_event_safely(
                session_factory,
                investigation_id=investigation_id,
                type="investigation.failed",
                payload={"error": error},
            )
        else:
            await self._remember(final, investigation_id, tenant_id)
        # otherwise the runner marks the row COMPLETED and writes investigation.completed

    async def _remember(self, final: Any, investigation_id: UUID, tenant_id: UUID) -> None:
        """Keep the conclusion for later investigations. Never fails the run: memory is a bonus."""
        root_cause = final.get("root_cause")
        incident = final.get("incident")
        if self._memory is None or root_cause is None or incident is None:
            return
        recommendation = final.get("recommendation")
        try:
            await self._memory.remember(
                NewMemory(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    incident_id=incident.incident_id,
                    service_name=incident.service_name,
                    root_cause=root_cause.statement,
                    recommendation=recommendation.summary if recommendation is not None else None,
                    confidence_band=final.get("confidence_band"),
                )
            )
        except Exception:  # embedding or database trouble: the investigation itself succeeded
            logger.warning("could not store the memory of investigation %s", investigation_id, exc_info=True)

    async def _await_gate(
        self,
        gate: Gate,
        session_factory: SessionFactory,
        investigation_id: UUID,
        tenant_id: UUID,
        request: dict[str, Any],
        *,
        announce: bool,
    ) -> None:
        """Mark the investigation as waiting at ``gate`` and (the first time) tell the stream why."""
        async with session_factory() as session:
            await investigation_store.transition_status(
                session, investigation_id=investigation_id, tenant_id=tenant_id, status=gate.paused
            )
        if announce:
            await event_store.append_event_safely(
                session_factory,
                investigation_id=investigation_id,
                type=gate.requested_event,
                payload=gate.announce(request),
            )

    @staticmethod
    async def _decision_for_gate(
        gate: Gate, session_factory: SessionFactory, tenant_id: UUID, investigation_id: UUID
    ) -> dict[str, Any] | None:
        """The first decision recorded after the latest request of this gate, or ``None``.

        The API writes the decided event before the run is resumed, so the event log is the durable
        hand-over: it survives a restart between the engineer's click and the resume. Taking the first
        decision after the *latest request* means an old round's decision is never reused and a
        racing second answer is ignored.
        """
        async with session_factory() as session:
            requested = await event_store.latest_of_type(
                session, tenant_id=tenant_id, investigation_id=investigation_id, type=gate.requested_event
            )
            events = await event_store.list_after(
                session, tenant_id=tenant_id, investigation_id=investigation_id, after_seq=requested.seq if requested else 0
            )
        for event in events:
            if event.type == gate.decided_event:
                return gate.resume_value(event.payload)
        return None

    async def _stream(
        self,
        graph_input: Any,
        config: Any,
        context: InvestigationContext,
        session_factory: SessionFactory,
        investigation_id: UUID,
    ) -> None:
        """Run the graph; every time a node finishes, persist what it produced as events."""
        async for update in self._graph.astream(graph_input, config, context=context, stream_mode="updates"):
            for event_type, payload in events_from_update(update):
                await event_store.append_event_safely(
                    session_factory,
                    investigation_id=investigation_id,
                    type=event_type,
                    payload=payload,
                )
