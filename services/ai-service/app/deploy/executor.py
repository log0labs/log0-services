"""Run one remediation through its graph: start it, resume it at a gate or when its timer is due, and record where it parked.

The runner owns concurrency (one run per remediation at a time, a cap overall) and the sweeper; this class owns the work in
between. It never decides anything about a deploy: the graph did. Its jobs are to pick the right way in (new run, mid-run
continue, resume at the approval gate, resume at a timer), to turn "the graph is parked here" into a status and a wake-up
time the database can answer questions about, and to say so in the investigation's event log.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.config import Settings
from app.deploy.graph import GRAPH_VERSION
from app.deploy.nodes import GATE, VERDICT_GATE, WAIT_GATE
from app.deploy.provider import DeployProvider
from app.deploy.state import (
    CheckReader,
    InstallationReader,
    RemediationContext,
    new_state,
    utcnow,
)
from app.integrations.github_deploys import DeploysPort
from app.runner import SessionFactory
from app.schemas.remediation import TERMINAL_STATUSES
from app.store import approvals
from app.store import events as event_store
from app.store import remediations as remediation_store
from app.tools.incidents import IncidentApi
from app.tools.logs import LogBackend

logger = logging.getLogger(__name__)


def pending_interrupts(snapshot: Any) -> list[Any]:
    return [interrupt for task in snapshot.tasks for interrupt in task.interrupts]


def thread_config(remediation_id: UUID) -> Any:
    """One checkpoint thread per remediation, in a namespace of its own (investigations use their bare id)."""
    return {"configurable": {"thread_id": f"remediation:{remediation_id}"}, "metadata": {"graph_version": GRAPH_VERSION}}


class RemediationExecutor:
    def __init__(
        self,
        graph: CompiledStateGraph,
        *,
        provider: DeployProvider,
        deploys: DeploysPort,
        checks: CheckReader,
        installations: InstallationReader,
        logs: LogBackend,
        incidents: IncidentApi,
        settings: Settings,
        now: Any = utcnow,
    ) -> None:
        self._graph = graph
        self._backends: dict[str, Any] = {
            "provider": provider,
            "deploys": deploys,
            "checks": checks,
            "installations": installations,
            "logs": logs,
            "incidents": incidents,
            "settings": settings,
            "now": now,
        }
        self._now = now

    async def run(self, session_factory: SessionFactory, remediation_id: UUID, tenant_id: UUID) -> None:
        async with session_factory() as session:
            row = await remediation_store.get(session, tenant_id=tenant_id, remediation_id=remediation_id)
        if row is None:
            raise LookupError(f"Remediation {remediation_id} not found for tenant {tenant_id}")
        if row.status in TERMINAL_STATUSES or row.status == "OFFERED":
            return  # nothing to run: finished, or an offer nobody started (a refused preflight leaves one)
        context = RemediationContext(tenant_id=tenant_id, sessions=session_factory, **self._backends)
        config = thread_config(remediation_id)
        snapshot = await self._graph.aget_state(config)
        waiting = pending_interrupts(snapshot)
        if waiting:
            resume = await self._resume_value(context, row, waiting[0].value)
            if resume is None:
                return  # not answered yet, or the timer is not due
            await self._graph.ainvoke(Command(resume=resume), config, context=context)
        elif snapshot.next:
            logger.info("Resuming remediation %s before %s", remediation_id, snapshot.next)
            await self._graph.ainvoke(None, config, context=context)
        elif not snapshot.values:
            state = new_state(remediation_id=remediation_id, investigation_id=row.investigation_id, environment=row.environment, merge_sha=row.merge_sha)
            await self._graph.ainvoke(state, config, context=context)
        # else: the graph already finished; only the bookkeeping below.

        snapshot = await self._graph.aget_state(config)
        waiting = pending_interrupts(snapshot)
        if waiting:
            await self._park(context, row, waiting[0].value)
            return
        async with session_factory() as session:
            await remediation_store.set_fields(session, tenant_id=tenant_id, remediation_id=remediation_id, wake_at=None)
            after = await remediation_store.get(session, tenant_id=tenant_id, remediation_id=remediation_id)
        if after is not None and after.status == "OFFERED":
            await self._forget_thread(remediation_id)  # a refused attempt is over: starting the offer again begins a clean run

    async def _forget_thread(self, remediation_id: UUID) -> None:
        checkpointer = self._graph.checkpointer
        if checkpointer:
            await checkpointer.adelete_thread(f"remediation:{remediation_id}")

    async def _resume_value(self, context: RemediationContext, row: remediation_store.Remediation, request: dict[str, Any]) -> dict[str, Any] | None:
        gate = request.get("gate") if isinstance(request, dict) else None
        if gate == GATE:
            async with context.sessions() as session:
                decision = await approvals.latest(session, tenant_id=context.tenant_id, remediation_id=row.id, gate="deploy")
            if decision is None:
                return None
            return {"action": decision.decision, "decided_by": str(decision.user_id), "role": decision.role}
        if gate == VERDICT_GATE:
            async with context.sessions() as session:
                decision = await approvals.nth(session, tenant_id=context.tenant_id, remediation_id=row.id, gate="decision", index=int(request.get("round", 0)))
            return {"action": decision.decision, "decided_by": str(decision.user_id), "role": decision.role} if decision else None
        if gate == WAIT_GATE:
            if row.wake_at is not None and row.wake_at > self._now():
                return None
            return {"tick": self._now().isoformat()}
        logger.warning("remediation %s is parked at an unknown gate %r", row.id, gate)
        return None

    async def _park(self, context: RemediationContext, row: remediation_store.Remediation, request: dict[str, Any]) -> None:
        """The graph stopped at a gate: make the database say so (a status for a human, a wake-up time for a timer)."""
        gate = request.get("gate")
        async with context.sessions() as session:
            if gate == GATE:
                won = await remediation_store.transition(
                    session, tenant_id=context.tenant_id, remediation_id=row.id, from_status="PREFLIGHT", to="AWAITING_DEPLOY_APPROVAL", wake_at=None
                )
                announce = won
            elif gate == VERDICT_GATE:
                won = await remediation_store.transition(
                    session, tenant_id=context.tenant_id, remediation_id=row.id, from_status="VERIFYING", to="AWAITING_DECISION", wake_at=None
                )
                announce = won
            elif gate == WAIT_GATE:
                wake_at = datetime.fromisoformat(str(request["wakeAt"])).astimezone(UTC)
                await remediation_store.set_fields(session, tenant_id=context.tenant_id, remediation_id=row.id, wake_at=wake_at)
                announce = False
            else:
                announce = False
        if announce:
            await event_store.append_event_safely(
                context.sessions,
                investigation_id=row.investigation_id,
                type="remediation.approval_requested" if gate == GATE else "remediation.decision_requested",
                payload={"remediationId": str(row.id), **{k: v for k, v in request.items() if k != "gate"}},
            )
