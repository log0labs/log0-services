"""Background investigation execution: concurrency, task registry, cooperative cancel."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.investigation import InvestigationStatus
from app.store import events as event_store
from app.store import investigations as investigation_store

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]
ExecuteFn = Callable[[SessionFactory, UUID, UUID], Awaitable[None]]

_TERMINAL = frozenset(
    {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
    }
)


async def default_investigation_execute(
    _session_factory: SessionFactory,
    _investigation_id: UUID,
    _tenant_id: UUID,
) -> None:
    """M1 graph stub - real LangGraph run replaces this in M2."""
    await asyncio.sleep(0)


class InvestigationRunner:
    """Runs investigations off the request path with a concurrency cap."""
    
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        max_concurrent: int = 4,
        execute: ExecuteFn | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._execute = execute or default_investigation_execute
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._shutting_down = False
    
    def schedule(self, investigation_id: UUID, tenant_id: UUID) -> None:
        """Schedule an investigation to run in the background."""
        if self.is_running(investigation_id):
            logger.warning("Investigation %s is already running", investigation_id)
            return
        task = asyncio.create_task(
            self._run(investigation_id, tenant_id=tenant_id),
            name=f"investigation-{investigation_id}",
        )
        self._tasks[investigation_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(investigation_id, None))
        logger.info("Scheduled investigation %s", investigation_id)
    
    def is_running(self, investigation_id: UUID) -> bool:
        """Check if an investigation is currently running."""
        task = self._tasks.get(investigation_id)
        return task is not None and not task.done()

    async def cancel(self, investigation_id: UUID, *, tenant_id: UUID) -> None:
        """Cancel a running investigation."""
        task = self._tasks.get(investigation_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("Investigation %s canceled", investigation_id)
        
        async with self._session_factory() as session:
            row = await investigation_store.get_by_id(
                session,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
            )
            if row is None or row.status in _TERMINAL:
                logger.info("Investigation %s is not running or already terminal", investigation_id)
                return
            await investigation_store.transition_status(
                session,
                investigation_id=investigation_id,
                tenant_id=tenant_id,
                status=InvestigationStatus.CANCELLED,
                set_completed=True,
            )
            await self._emit(investigation_id, "investigation.cancelled")
    
    async def recover_on_startup(self) -> None:
        """Recover any investigations that were running when the service stopped."""
        try:
            async with asyncio.timeout(5):
                async with self._session_factory() as session:
                    pending = await investigation_store.list_pending_for_runner(session)
        except Exception:
            logger.exception("Startup recovery skipped: could not list pending investigations")
            return
        for investigation_id, tenant_id in pending:
            logger.info(
                "Recovering investigation %s (tenant %s) on startup",
                investigation_id,
                tenant_id,
            )
            self.schedule(investigation_id, tenant_id)
    
    async def shutdown(self) -> None:
        """Stop all tasks but leave their rows RUNNING so recover_on_startup resumes them."""
        self._shutting_down = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
    
    async def _emit(
        self,
        investigation_id: UUID,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        """Best-effort event write; a failed event must never fail the investigation."""
        try:
            async with self._session_factory() as session:
                await event_store.append_event(
                    session,
                    investigation_id=investigation_id,
                    type=event_type,
                    payload=payload,
                )
        except Exception:
            logger.exception("Could not write event %s for %s", event_type, investigation_id)
    
    async def _run(self, investigation_id: UUID, *, tenant_id: UUID) -> None: 
        async with self._semaphore:
            try:
                async with self._session_factory() as session:
                    started = await investigation_store.mark_running(
                          session,
                          investigation_id=investigation_id,
                          tenant_id=tenant_id,
                      )
                if not started:
                    logger.warning(
                        "Investigation %s (tenant %s) was not in a startable state",
                        investigation_id,
                        tenant_id,
                    )
                    return
                await self._emit(investigation_id, "investigation.started")
                await self._execute(
                    self._session_factory,
                    investigation_id,
                    tenant_id,
                )

                async with self._session_factory() as session:
                    row = await investigation_store.get_by_id(
                        session,
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                    )
                    if row is not None and row.status not in _TERMINAL:
                        await investigation_store.transition_status(
                            session,
                            investigation_id=investigation_id,
                            tenant_id=tenant_id,
                            status=InvestigationStatus.COMPLETED,
                            set_completed=True,
                        )
                        await self._emit(investigation_id, "investigation.completed")
            except asyncio.CancelledError:
                if self._shutting_down:
                    logger.info(
                        "Investigation %s interrupted by shutdown; left for recovery",
                        investigation_id,
                    )
                    raise
                logger.info(
                    "Investigation %s (tenant %s) was canceled",
                    investigation_id,
                    tenant_id,
                )
                async with self._session_factory() as session:
                    await investigation_store.transition_status(
                        session,
                        investigation_id=investigation_id,
                        tenant_id=tenant_id,
                        status=InvestigationStatus.CANCELLED,
                        set_completed=True,
                    )
                await self._emit(investigation_id, "investigation.cancelled")
                raise
            except Exception as exc:
                logger.exception("Investigation %s failed", investigation_id)
                async with self._session_factory() as session:
                    await investigation_store.transition_status(
                        session,
                        investigation_id=investigation_id,
                        tenant_id=tenant_id,
                        status=InvestigationStatus.FAILED,
                        error=str(exc),
                        set_completed=True,
                    )
                await self._emit(investigation_id, "investigation.failed", {"error": str(exc)})
        