"""LangSmith / LangChain tracing setup.

pydantic-settings loads ``LANGCHAIN_*`` into :class:`app.config.Settings` only.
The LangSmith SDK reads ``os.environ``, so we mirror settings there at startup.
"""

from __future__ import annotations

import logging
import os
from contextlib import AbstractContextManager, nullcontext
from uuid import UUID

from langsmith import tracing_context

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


def tracing_allowed(settings: Settings, *, tenant_id: UUID, byok: bool) -> bool:
    """May this tenant's investigation be sent to LangSmith?

    Only when tracing is on at all, and then: always for tenants using the platform's own keys, and for
    bring-your-own-key tenants only if they are in ``tracing_opt_in_tenants``. A trace contains the prompts
    and answers (already redacted, see ``security/redaction.py``), so the default for data that came with
    the tenant's own provider contract is: not shared.
    """
    if not settings.langchain_tracing_v2:
        return False
    return not byok or tenant_id in settings.tracing_opt_in_tenants


def tracing_scope(allowed: bool) -> AbstractContextManager[object]:
    """Switch tracing off for the code run inside this block when it is not allowed; otherwise change nothing."""
    return nullcontext() if allowed else tracing_context(enabled=False)
