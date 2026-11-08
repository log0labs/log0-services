"""FastAPI entry point: ``uvicorn app.main:app``."""

import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api import health, investigations, summaries
from app.config import get_settings
from app.db.session import create_db_engine, create_session_factory
from app.graph.builder import build_graph
from app.graph.checkpoint import open_checkpointer
from app.graph.executor import GraphExecutor
from app.integrations.clickhouse import LogStore
from app.integrations.incident_db import IncidentReader
from app.integrations.incident_service import IncidentApiClient
from app.llm.models import PurposeChatModels
from app.observability import configure_tracing
from app.runner import InvestigationRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    configure_tracing(settings)
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    app.state.db_engine = engine
    app.state.async_session_factory = session_factory
    log_store = LogStore(settings)
    app.state.log_store = log_store
    incident_reader = IncidentReader(settings)
    app.state.incident_reader = incident_reader
    incident_api = IncidentApiClient(settings)
    app.state.incident_api = incident_api

    async with AsyncExitStack() as stack:
        executor: GraphExecutor | None = None
        if settings.investigation_graph_enabled:
            # Opens the Postgres checkpointer (creating its tables on first start) and keeps it
            # open until the stack unwinds, which is after the runner has stopped.
            saver = await stack.enter_async_context(open_checkpointer(settings))
            executor = GraphExecutor(
                build_graph(saver),
                logs=log_store,
                incidents=incident_api,
                history=incident_reader,
                models=PurposeChatModels(settings),
            )
        app.state.graph_executor = executor
        runner = InvestigationRunner(
            session_factory,
            max_concurrent=settings.investigation_max_concurrent,
            execute=executor.run if executor else None,  # None: the runner's no-op stub
        )
        app.state.investigation_runner = runner
        await runner.recover_on_startup()
        yield
        await runner.shutdown()

    await log_store.close()
    await incident_reader.close()
    await engine.dispose()


def create_app() -> FastAPI:
    """Build and wire the FastAPI application."""
    app = FastAPI(
        title="log0 AI Service",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.include_router(health.router)
    app.include_router(summaries.router)
    app.include_router(investigations.router)
    return app


app = create_app()
