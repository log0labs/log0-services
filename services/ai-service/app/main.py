"""FastAPI entry point: ``uvicorn app.main:app``."""

import logging

from fastapi import FastAPI

from app.api import health, summaries

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

def create_app() -> FastAPI:
    """Build and wire the FastAPI application."""
    app = FastAPI(title="log0 AI Service", version="0.1.0")
    app.include_router(health.router)
    app.include_router(summaries.router)
    return app


app = create_app()
