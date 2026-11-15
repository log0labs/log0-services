"""One retry for a model that asked for a tool it was not given.

Some models (seen with ``gpt-oss`` on Groq) occasionally invent a tool such as ``apply_patch``. The provider rejects the
whole reply with a 400 ``tool_use_failed``; asking again usually works. Only that error is retried, up to three attempts in all: rate limits,
timeouts and outages are not, because repeating them only burns quota or time.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.exceptions import OutputParserException

logger = logging.getLogger(__name__)

_MARKERS = (
    "tool_use_failed",
    "failed to call a function",
    "tool call validation failed",
    "which was not in request.tools",
)


def is_tool_call_rejection(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _MARKERS)


TOOL_REJECTION_ATTEMPTS = 3


def unwrap_schema_envelope(raw: str | None) -> dict[str, Any] | None:
    """Some models answer a structured request with the JSON schema's own shape, putting the real answer under
    ``"properties"``: ``{"description": ..., "properties": {"changes": [...]}}``. The data inside is fine, so take it
    out. Returns None when the text is not that shape (or when ``properties`` really is a schema, not data)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    props = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(props, dict) or not props:
        return None
    if all(isinstance(v, dict) and "type" in v for v in props.values()):
        return None  # this is a schema describing fields, not an answer
    return props


async def invoke_with_tool_retry(call: Callable[[], Awaitable[Any]], *, timeout: float, what: str) -> Any:
    """Run ``call`` under ``timeout``. A rejected tool call or an answer that cannot be parsed is tried again (up to
    3 attempts); an answer wrapped in a schema envelope is unwrapped instead. Anything else is raised at once."""
    for attempt in range(1, TOOL_REJECTION_ATTEMPTS + 1):
        try:
            async with asyncio.timeout(timeout):
                return await call()
        except OutputParserException as exc:
            recovered = unwrap_schema_envelope(exc.llm_output)
            if recovered is not None:
                logger.warning("%s: the answer came wrapped in a schema envelope, unwrapped it", what)
                return recovered
            if attempt == TOOL_REJECTION_ATTEMPTS:
                raise
            logger.warning("%s: the answer could not be parsed, trying again (%d of %d)", what, attempt, TOOL_REJECTION_ATTEMPTS)
        except Exception as exc:
            if attempt == TOOL_REJECTION_ATTEMPTS or not is_tool_call_rejection(exc):
                raise
            logger.warning(
                "%s: the provider rejected a tool call, trying again (%d of %d): %s", what, attempt, TOOL_REJECTION_ATTEMPTS, exc
            )
    raise AssertionError("unreachable")
