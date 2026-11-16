"""Try a model with a key: the "Test connection" button of the AI settings page.

Sends one tiny prompt and reports whether it worked, how long it took and, when it did not, a
short *category* of failure. The provider's own error text is deliberately not returned: it can
quote the request, and the request carries the key being tested.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.llm.factory import GenerationConfig, build_chat_model

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 20
_PROMPT = "Reply with the single word OK."


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    latency_ms: int
    error: str | None = None  # auth | model_not_found | rate_limited | timeout | provider_unavailable | error


# provider, model, api key -> result. The API takes it as a dependency so tests never call a provider.
ConnectionChecker = Callable[[str, str, str], Awaitable[ConnectionCheck]]


def classify(exc: BaseException) -> str:
    """Map a provider SDK exception to a category, using the HTTP status the SDKs expose."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "model_not_found"
    if status == 429:
        return "rate_limited"
    if isinstance(status, int) and status >= 500:
        return "provider_unavailable"
    if "timeout" in type(exc).__name__.lower():
        return "timeout"
    return "error"


async def check_connection(provider: str, model: str, api_key: str) -> ConnectionCheck:
    started = time.monotonic()
    try:
        chat = build_chat_model(
            provider=provider,
            model=model,
            api_key=api_key,
            generation=GenerationConfig(temperature=0.0, max_tokens=16, timeout_seconds=CHECK_TIMEOUT_SECONDS),
        )
        await asyncio.wait_for(chat.ainvoke(_PROMPT), timeout=CHECK_TIMEOUT_SECONDS + 5)
    except Exception as exc:  # noqa: BLE001 - any failure is a result here, not a crash
        category = classify(exc)
        logger.info("connection check failed (%s): %s", provider, type(exc).__name__)  # class only, never the message
        return ConnectionCheck(ok=False, latency_ms=_ms(started), error=category)
    return ConnectionCheck(ok=True, latency_ms=_ms(started))


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
