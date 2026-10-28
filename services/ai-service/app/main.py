"""FastAPI entry point: ``uvicorn app.main:app``."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, investigations, summaries
from app.config import get_settings
from app.db.session import create_db_engine, create_session_factory
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
    runner = InvestigationRunner(
        session_factory,
        max_concurrent=settings.investigation_max_concurrent,
    )
    app.state.investigation_runner = runner
    await runner.recover_on_startup()
    yield
    await runner.shutdown()
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
