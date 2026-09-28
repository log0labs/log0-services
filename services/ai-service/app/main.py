"""FastAPI entry point: ``uvicorn app.main:app``."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, summaries
from app.config import get_settings
from app.observability import configure_tracing

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    configure_tracing(get_settings())
    yield


def create_app() -> FastAPI:
    """Build and wire the FastAPI application."""
    app = FastAPI(
        title="log0 AI Service",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.include_router(health.router)
    app.include_router(summaries.router)
    return app


app = create_app()
