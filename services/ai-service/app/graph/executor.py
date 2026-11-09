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

from app.graph.builder import GRAPH_VERSION
from app.graph.code_access import CodeAccess
from app.graph.events import events_from_update
from app.graph.result import result_for_row
from app.graph.state import ChatModels, InvestigationContext, new_state
from app.graph.usage import Prices, UsageRecorder, run_config
from app.runner import SessionFactory
from app.schemas.investigation import InvestigationStatus
from app.store import events as event_store
from app.store import investigations as investigation_store
from app.tools.incidents import IncidentApi, RelatedIncidentSource
from app.tools.logs import LogBackend

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._graph = graph
        self._logs = logs
        self._incidents = incidents
        self._history = history
        self._models = models
        self._prices = prices or {}
        self._code = code

    async def run(self, session_factory: SessionFactory, investigation_id: UUID, tenant_id: UUID) -> None:
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
            models=self._models,
            code=self._code,
        )

        snapshot = await self._graph.aget_state(config)
        if snapshot.next:
            # The service stopped mid-run (restart, deploy): continue from the last checkpoint,
            # so finished nodes are not repeated and their tokens are not spent twice.
            logger.info("Resuming investigation %s before %s", investigation_id, snapshot.next)
            await self._stream(None, config, context, session_factory, investigation_id)
        elif not snapshot.values:
            state = new_state(
                investigation_id=str(investigation_id),
                incident_id=str(row.incident_id),
                scope=row.scope,
                question=row.question,
            )
            await self._stream(state, config, context, session_factory, investigation_id)
        # else: the graph already finished but the service stopped before the row was updated;
        # nothing to run, only the final bookkeeping below.

        final = (await self._graph.aget_state(config)).values
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
        # otherwise the runner marks the row COMPLETED and writes investigation.completed

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
