"""LangSmith / LangChain tracing setup.

pydantic-settings loads ``LANGCHAIN_*`` into :class:`app.config.Settings` only.
The LangSmith SDK reads ``os.environ``, so we mirror settings there at startup.
"""

from __future__ import annotations

import logging
import os

from app.config import Settings

logger = logging.getLogger(__name__)


def configure_tracing(settings: Settings) -> None:
    """Export LangSmith configuration from Settings into ``os.environ``."""
    enabled = settings.langchain_tracing_v2
    os.environ["LANGCHAIN_TRACING_V2"] = "true" if enabled else "false"

    if settings.langchain_endpoint:
        os.environ["LANGCHAIN_ENDPOINT"] = settings.langchain_endpoint

    if settings.langchain_project:
        os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project

    if settings.langchain_api_key is not None:
        os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key.get_secret_value()
    elif enabled:
        logger.warning(
            "LANGCHAIN_TRACING_V2 is true but LANGCHAIN_API_KEY is unset; "
            "LangSmith traces will not export."
        )
