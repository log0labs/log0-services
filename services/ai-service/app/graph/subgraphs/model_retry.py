"""One retry for a model that asked for a tool it was not given.

Some models (seen with ``gpt-oss`` on Groq) occasionally invent a tool such as ``apply_patch``. The provider rejects the
whole reply with a 400 ``tool_use_failed``; asking again usually works. Only that error is retried, once: rate limits,
timeouts and outages are not, because repeating them only burns quota or time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

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


async def invoke_with_tool_retry(
    call: Callable[[], Awaitable[Any]], *, timeout: float, what: str
) -> Any:
    """Run ``call`` under ``timeout``; if the provider rejected the model's tool call, run it once more."""
    try:
        async with asyncio.timeout(timeout):
            return await call()
    except Exception as exc:
        if not is_tool_call_rejection(exc):
            raise
        logger.warning(
            "%s: the provider rejected a tool call, retrying once: %s", what, exc
        )
    async with asyncio.timeout(timeout):
        return await call()
