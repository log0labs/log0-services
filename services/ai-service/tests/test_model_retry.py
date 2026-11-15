import pytest
from langchain_core.exceptions import OutputParserException

from app.graph.subgraphs.model_retry import (
    invoke_with_tool_retry,
    is_tool_call_rejection,
    unwrap_schema_envelope,
)


class _Rejected(Exception):
    pass


def test_recognises_the_groq_rejection():
    err = _Rejected(
        "Error code: 400 - {'error': {'code': 'tool_use_failed', 'message': 'attempted to call tool apply_patch which was not in request.tools'}}"
    )
    assert is_tool_call_rejection(err)


@pytest.mark.parametrize(
    "message", ["Error code: 429 rate_limit_exceeded", "connection reset", "timed out"]
)
def test_other_errors_are_not_retried_candidates(message):
    assert not is_tool_call_rejection(RuntimeError(message))


@pytest.mark.asyncio
async def test_retries_after_a_tool_rejection():
    calls = []

    async def call():
        calls.append(1)
        if len(calls) == 1:
            raise _Rejected("tool_use_failed")
        return "ok"

    assert await invoke_with_tool_retry(call, timeout=5, what="t") == "ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_gives_up_after_three_attempts():
    calls = []

    async def call():
        calls.append(1)
        raise _Rejected("tool_use_failed")

    with pytest.raises(_Rejected):
        await invoke_with_tool_retry(call, timeout=5, what="t")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_rate_limit_is_not_retried():
    calls = []

    async def call():
        calls.append(1)
        raise RuntimeError("429 rate_limit_exceeded")

    with pytest.raises(RuntimeError):
        await invoke_with_tool_retry(call, timeout=5, what="t")
    assert len(calls) == 1


def test_unwraps_an_answer_the_model_put_under_properties():
    raw = '{"description": "Fix it.", "properties": {"changes": [{"file_path": "a.go"}], "notes": []}, "type": "object"}'
    assert unwrap_schema_envelope(raw) == {"changes": [{"file_path": "a.go"}], "notes": []}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not json",
        '{"changes": []}',  # already the answer
        '{"properties": {"changes": {"type": "array"}, "notes": {"type": "array"}}}',  # a schema, not data
        "[1, 2]",
    ],
)
def test_leaves_everything_else_alone(raw):
    assert unwrap_schema_envelope(raw) is None


@pytest.mark.asyncio
async def test_a_wrapped_answer_is_recovered_without_asking_again():
    calls = []

    async def call():
        calls.append(1)
        raise OutputParserException("bad", llm_output='{"properties": {"changes": []}}')

    assert await invoke_with_tool_retry(call, timeout=5, what="t") == {"changes": []}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_unparseable_answer_is_tried_again_then_raised():
    calls = []

    async def call():
        calls.append(1)
        raise OutputParserException("bad", llm_output="nonsense")

    with pytest.raises(OutputParserException):
        await invoke_with_tool_retry(call, timeout=5, what="t")
    assert len(calls) == 3
