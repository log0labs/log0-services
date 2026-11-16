"""Background lifecycle of remediations: run one at a time per remediation, wake the ones whose timer is due.

``schedule`` starts a run as a task. If that remediation is already running (a click arrived while a poll was in progress)
the request is remembered and the run repeats once the current one ends, so no answer is lost and two runs never overlap.
The sweeper is a loop that asks the database which waiting runs are due and schedules them; because the wake-up time is a
column and not memory, a restart loses nothing. A run that dies with an unexpected error is moved to FAILED so that it
never keeps the environment locked.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from app.deploy.state import utcnow
from app.runner import SessionFactory
from app.store import deploy_audit
from app.store import remediations as remediation_store

logger = logging.getLogger(__name__)

ExecuteFn = Callable[[SessionFactory, UUID, UUID], Awaitable[None]]


class RemediationRunner:
    def __init__(self, session_factory: SessionFactory, execute: ExecuteFn, *, max_concurrent: int = 4, sweep_seconds: float = 10.0, now: Callable[[], datetime] = utcnow) -> None:
        self._sessions = session_factory
        self._execute = execute
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._sweep_seconds = sweep_seconds
        self._now = now
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._again: set[UUID] = set()
        self._sweeper: asyncio.Task[None] | None = None

    def schedule(self, remediation_id: UUID, tenant_id: UUID) -> None:
        if remediation_id in self._tasks and not self._tasks[remediation_id].done():
            self._again.add(remediation_id)
            return
        task = asyncio.create_task(self._loop(remediation_id, tenant_id), name=f"remediation:{remediation_id}")
        self._tasks[remediation_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(remediation_id, None))

    def is_running(self, remediation_id: UUID) -> bool:
        task = self._tasks.get(remediation_id)
        return task is not None and not task.done()

    async def wait_idle(self, remediation_id: UUID, *, timeout: float = 5.0) -> bool:
        task = self._tasks.get(remediation_id)
        if task is None or task.done():
            return True
        done, _ = await asyncio.wait({task}, timeout=timeout)
        return bool(done)

    async def _loop(self, remediation_id: UUID, tenant_id: UUID) -> None:
        while True:
            self._again.discard(remediation_id)
            try:
                async with self._semaphore:
                    await self._execute(self._sessions, remediation_id, tenant_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("remediation %s failed unexpectedly", remediation_id)
                await self._fail(remediation_id, tenant_id, type(exc).__name__)
                return
            if remediation_id not in self._again:
                return

    async def _fail(self, remediation_id: UUID, tenant_id: UUID, error: str) -> None:
        try:
            async with self._sessions() as session:
                moved = await remediation_store.transition(
                    session, tenant_id=tenant_id, remediation_id=remediation_id, to="FAILED", outcome={"failed": "log0 hit an internal error", "error": error}
                )
                if moved:
                    await deploy_audit.append(session, tenant_id=tenant_id, remediation_id=remediation_id, action="remediation.crashed", result="failed", detail=error)
        except Exception:
            logger.exception("could not record the failure of remediation %s", remediation_id)

    async def sweep_once(self) -> int:
        """Schedule every waiting run whose wake-up time has come; returns how many."""
        async with self._sessions() as session:
            due = await remediation_store.due(session, now=self._now())
        for remediation_id, tenant_id in due:
            self.schedule(remediation_id, tenant_id)
        return len(due)

    async def recover_on_startup(self) -> int:
        """Pick up runs that were mid-flight when the service stopped."""
        async with self._sessions() as session:
            pending = await remediation_store.interrupted(session)
        for remediation_id, tenant_id in pending:
            self.schedule(remediation_id, tenant_id)
        return len(pending)

    def start_sweeper(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_forever(), name="remediation-sweeper")

    async def _sweep_forever(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("remediation sweep failed")
            await asyncio.sleep(self._sweep_seconds)

    async def shutdown(self) -> None:
        tasks = [t for t in (self._sweeper, *self._tasks.values()) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._sweeper = None
