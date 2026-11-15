"""One retry for a model that asked for a tool it was not given.

Some models (seen with ``gpt-oss`` on Groq) occasionally invent a tool such as ``apply_patch``. The provider rejects the
whole reply with a 400 ``tool_use_failed``; asking again usually works. Only that error is retried, up to three attempts in all: rate limits,
timeouts and outages are not, because repeating them only burns quota or time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
RATE_LIMIT_WAITS = 3
MAX_RATE_LIMIT_WAIT_SECONDS = 90.0  # a per-minute limit clears within a minute; a daily one (hours) is not waited for

_sleep = asyncio.sleep  # a name tests can replace

_RETRY_SECONDS = (
    re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s"),  # Gemini: 'retryDelay': '34s'
    re.compile(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s"),  # Groq: "try again in 7.5s" or "in 2m5s"
)


def rate_limit_delay(exc: BaseException) -> float | None:
    """How long the provider asked us to wait after a rate-limit (429) answer, in seconds; None when the
    error is not a rate limit or the wait is too long to be a per-minute limit (a daily cap is not waited for)."""
    text = str(exc)
    if "429" not in text and "RESOURCE_EXHAUSTED" not in text and "rate limit" not in text.lower():
        return None
    delay: float | None = None
    match = _RETRY_SECONDS[0].search(text)
    if match:
        delay = float(match.group(1))
    else:
        match = _RETRY_SECONDS[1].search(text)
        if match:
            delay = float(match.group(1) or 0) * 60 + float(match.group(2))
    if delay is None or delay > MAX_RATE_LIMIT_WAIT_SECONDS:
        return None
    return delay + 1.0  # a second of margin


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
    """Run ``call`` under ``timeout``. A rejected tool call or an unparseable answer is tried again (up to 3 attempts);
    an answer wrapped in a schema envelope is unwrapped; a short per-minute rate limit is waited out as long as the
    provider says (up to 3 times). Anything else is raised at once."""
    attempt = 1
    waits = 0
    while True:
        try:
            async with asyncio.timeout(timeout):
                return await call()
        except OutputParserException as exc:
            recovered = unwrap_schema_envelope(exc.llm_output)
            if recovered is not None:
                logger.warning("%s: the answer came wrapped in a schema envelope, unwrapped it", what)
                return recovered
            if attempt >= TOOL_REJECTION_ATTEMPTS:
                raise
            logger.warning("%s: the answer could not be parsed, trying again (%d of %d)", what, attempt, TOOL_REJECTION_ATTEMPTS)
            attempt += 1
        except Exception as exc:
            delay = rate_limit_delay(exc)
            if delay is not None and waits < RATE_LIMIT_WAITS:
                waits += 1
                logger.warning("%s: rate limited, waiting %.0fs before trying again (%d of %d)", what, delay, waits, RATE_LIMIT_WAITS)
                await _sleep(delay)
                continue
            if attempt >= TOOL_REJECTION_ATTEMPTS or not is_tool_call_rejection(exc):
                raise
            logger.warning(
                "%s: the provider rejected a tool call, trying again (%d of %d): %s", what, attempt, TOOL_REJECTION_ATTEMPTS, exc
            )
            attempt += 1
