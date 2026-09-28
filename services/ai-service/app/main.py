"""FastAPI entry point: ``uvicorn app.main:app``."""

from fastapi import FastAPI

from app.api import health


def create_app() -> FastAPI:
    """Build and wire the FastAPI application."""
    app = FastAPI(title="log0 AI Service", version="0.1.0")
    app.include_router(health.router)
    return app


app = create_app()
