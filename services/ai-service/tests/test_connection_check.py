"""The "Test connection" check: success, categories of failure, and no secrets in what it reports."""

import asyncio
import logging
from typing import Any

import pytest

from app.llm import connection_check
from app.llm.connection_check import check_connection, classify

pytestmark = pytest.mark.asyncio

API_KEY = "gsk_live_0123456789abcdefABCD"


class _StatusError(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class _FakeChat:
    def __init__(self, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.error, self.delay = error, delay
        self.prompts: list[Any] = []

    async def ainvoke(self, prompt: Any) -> str:
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return "OK"


def _use(monkeypatch: pytest.MonkeyPatch, chat: _FakeChat) -> list[dict[str, Any]]:
    built: list[dict[str, Any]] = []

    def fake_build(**kwargs: Any) -> _FakeChat:
        built.append(kwargs)
        return chat

    monkeypatch.setattr(connection_check, "build_chat_model", fake_build)
    return built


async def test_a_working_model_is_ok_with_a_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _FakeChat()
    built = _use(monkeypatch, chat)

    result = await check_connection("groq", "openai/gpt-oss-120b", API_KEY)

    assert result.ok and result.error is None and result.latency_ms >= 0
    assert (built[0]["provider"], built[0]["model"], built[0]["api_key"]) == ("groq", "openai/gpt-oss-120b", API_KEY)
    assert built[0]["generation"].max_tokens == 16  # a tiny, cheap request
    assert len(chat.prompts) == 1


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (_StatusError(401), "auth"),
        (_StatusError(403), "auth"),
        (_StatusError(404), "model_not_found"),
        (_StatusError(429), "rate_limited"),
        (_StatusError(500), "provider_unavailable"),
        (_StatusError(503), "provider_unavailable"),
        (_StatusError(400), "error"),
        (TimeoutError(), "timeout"),
        (type("APITimeoutError", (Exception,), {})(), "timeout"),
        (RuntimeError("boom"), "error"),
    ],
)
async def test_failures_are_reported_as_a_category(monkeypatch: pytest.MonkeyPatch, exc: BaseException, category: str) -> None:
    _use(monkeypatch, _FakeChat(error=exc))
    result = await check_connection("groq", "m", API_KEY)
    assert (result.ok, result.error) == (False, category)
    assert classify(exc) == category


async def test_the_providers_error_text_is_never_returned_or_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _use(monkeypatch, _FakeChat(error=_StatusError(401, f"Incorrect API key provided: {API_KEY}")))
    with caplog.at_level(logging.DEBUG):
        result = await check_connection("groq", "m", API_KEY)
    assert API_KEY not in repr(result) and API_KEY not in caplog.text


async def test_a_model_that_cannot_even_be_built_is_a_failed_check_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(**kwargs: Any) -> None:
        raise ValueError("bad configuration")

    monkeypatch.setattr(connection_check, "build_chat_model", broken)
    result = await check_connection("groq", "m", API_KEY)
    assert (result.ok, result.error) == (False, "error")


async def test_a_call_that_never_answers_ends_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_check, "CHECK_TIMEOUT_SECONDS", 0)  # the guard becomes 5 s: patch it down further
    _use(monkeypatch, _FakeChat(delay=30))
    original = asyncio.wait_for

    async def quick(awaitable: Any, timeout: float) -> Any:
        return await original(awaitable, timeout=0.05)

    monkeypatch.setattr(connection_check.asyncio, "wait_for", quick)
    result = await check_connection("groq", "m", API_KEY)
    assert (result.ok, result.error) == (False, "timeout")
