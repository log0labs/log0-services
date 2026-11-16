"""The usage recorder: tokens, cost and timing of one run. A fake chat model reports usage; no network."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.config import Settings
from app.graph.executor import usage_for_row
from app.graph.usage import LlmCall, UsageRecorder, run_config

INVESTIGATION = UUID("c9d8e7f6-a5b4-4c3d-8e1f-0a9b8c7d6e5f")


class _State(TypedDict):
    answer: str


def _reply(*, model: str = "model-a", tokens_in: int = 100, tokens_out: int = 20) -> AIMessage:
    return AIMessage(
        content="ok",
        usage_metadata={"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out},
        response_metadata={"model_name": model},
    )


async def _run(replies: list[AIMessage], recorder: UsageRecorder, *, nodes: tuple[str, ...] = ("ask",)) -> None:
    """A tiny graph whose nodes each make one model call, run with the recorder attached."""
    chat = FakeMessagesListChatModel(responses=replies)
    builder = StateGraph(_State)

    def make(name: str) -> Any:
        async def node(state: _State) -> dict[str, str]:
            await chat.ainvoke([("system", "be brief"), ("human", "hello there")])
            return {"answer": name}

        return node

    previous: str = START
    for name in nodes:
        builder.add_node(name, make(name))
        builder.add_edge(previous, name)
        previous = name
    builder.add_edge(previous, END)
    await builder.compile().ainvoke({"answer": ""}, {"callbacks": [recorder]})


# --------------------------------------------------------------------- the recorder


@pytest.mark.asyncio
async def test_each_call_is_attributed_to_its_graph_node_with_tokens_model_and_prompt_size() -> None:
    recorder = UsageRecorder()
    await _run([_reply(tokens_in=120, tokens_out=30), _reply(model="model-b", tokens_in=50, tokens_out=10)], recorder, nodes=("first", "second"))

    assert [(c.node, c.model, c.input_tokens, c.output_tokens) for c in recorder.calls] == [
        ("first", "model-a", 120, 30),
        ("second", "model-b", 50, 10),
    ]
    assert all(c.ok and c.seconds >= 0 and c.prompt_chars == len("be brief") + len("hello there") for c in recorder.calls)
    assert (recorder.input_tokens, recorder.output_tokens, recorder.has_usage) == (170, 40, True)


@pytest.mark.asyncio
async def test_every_node_is_timed_and_helper_chains_inside_it_are_not_counted() -> None:
    recorder = UsageRecorder()
    await _run([_reply(), _reply()], recorder, nodes=("first", "second"))
    assert [n.node for n in recorder.nodes] == ["first", "second"]
    assert all(n.seconds >= 0 for n in recorder.nodes)


@pytest.mark.asyncio
async def test_a_failed_call_is_recorded_but_adds_no_tokens() -> None:
    class Broken(FakeMessagesListChatModel):
        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    recorder = UsageRecorder()
    builder = StateGraph(_State)

    async def node(state: _State) -> dict[str, str]:
        await Broken(responses=[_reply()]).ainvoke([("human", "hi")])
        return {"answer": "x"}

    builder.add_node("ask", node)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    with pytest.raises(RuntimeError, match="provider down"):
        await builder.compile().ainvoke({"answer": ""}, {"callbacks": [recorder]})

    assert [(c.node, c.ok, c.input_tokens) for c in recorder.calls] == [("ask", False, 0)]
    assert not recorder.has_usage and recorder.cost_usd() is None
    assert [n.node for n in recorder.nodes] == ["ask"]  # the node that failed is still timed


@pytest.mark.asyncio
async def test_a_provider_that_reports_no_usage_leaves_the_totals_unknown() -> None:
    recorder = UsageRecorder({"model-a": (1.0, 2.0)})
    await _run([AIMessage(content="ok", response_metadata={"model_name": "model-a"})], recorder)
    assert recorder.calls and not recorder.has_usage
    assert recorder.cost_usd() == Decimal("0.000000")  # priced, but nothing was reported


def test_a_recorder_that_saw_nothing_has_no_usage_and_no_cost() -> None:
    recorder = UsageRecorder({"m": (1.0, 1.0)})
    assert (recorder.input_tokens, recorder.output_tokens) == (0, 0)
    assert not recorder.has_usage and recorder.cost_usd() is None


# ------------------------------------------------------------------------------ cost


def _recorder_with(calls: list[LlmCall], prices: dict[str, tuple[float, float]]) -> UsageRecorder:
    recorder = UsageRecorder(prices)
    recorder.calls.extend(calls)
    return recorder


def _call(model: str, tokens_in: int, tokens_out: int, *, ok: bool = True) -> LlmCall:
    return LlmCall("n", model, tokens_in, tokens_out, 0.1, 10, ok)


def test_cost_is_tokens_times_the_configured_price_per_million() -> None:
    recorder = _recorder_with([_call("m", 2_000_000, 1_000_000)], {"m": (0.15, 0.75)})
    assert recorder.cost_usd() == Decimal("1.050000")  # 2 * 0.15 + 1 * 0.75


def test_cost_adds_up_over_models_and_is_rounded_to_six_places() -> None:
    recorder = _recorder_with([_call("a", 1234, 567), _call("b", 100, 100)], {"a": (0.15, 0.75), "b": (1.0, 2.0)})
    expected = (Decimal(1234) * Decimal("0.15") + Decimal(567) * Decimal("0.75") + Decimal(100) * 1 + Decimal(100) * 2) / 1_000_000
    assert recorder.cost_usd() == expected.quantize(Decimal("0.000001"))


def test_one_unpriced_model_makes_the_whole_cost_unknown_not_partly_wrong() -> None:
    recorder = _recorder_with([_call("a", 10, 10), _call("unpriced", 10, 10)], {"a": (1.0, 1.0)})
    assert recorder.cost_usd() is None


def test_failed_calls_do_not_need_a_price() -> None:
    recorder = _recorder_with([_call("a", 1_000_000, 0), _call("unknown", 0, 0, ok=False)], {"a": (2.0, 0.0)})
    assert recorder.cost_usd() == Decimal("2.000000")


def test_prices_come_from_the_llm_prices_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PRICES", '{"openai/gpt-oss-120b": [0.15, 0.75]}')
    assert Settings(_env_file=None).llm_prices == {"openai/gpt-oss-120b": (0.15, 0.75)}  # type: ignore[call-arg]
    monkeypatch.delenv("LLM_PRICES")
    assert Settings(_env_file=None).llm_prices == {}  # type: ignore[call-arg]  # no guessed prices


# ------------------------------------------------------------------------- the row


def test_what_goes_on_the_row_is_the_totals_or_nothing_at_all() -> None:
    assert usage_for_row(UsageRecorder()) == {}  # nothing measured: the columns stay empty
    assert usage_for_row(_recorder_with([_call("a", 0, 0)], {})) == {}  # a call that reported no tokens

    row = usage_for_row(_recorder_with([_call("a", 1_000_000, 1_000_000)], {"a": (1.0, 2.0)}))
    assert row == {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cost_usd": Decimal("3.000000")}
    unpriced = usage_for_row(_recorder_with([_call("a", 5, 6)], {}))
    assert unpriced == {"input_tokens": 5, "output_tokens": 6, "cost_usd": None}  # tokens known, cost not


# ---------------------------------------------------------------------------- config


def test_the_run_config_names_the_run_and_carries_no_tenant_or_incident_data() -> None:
    recorder = UsageRecorder()
    config = run_config(INVESTIGATION, recorder, "m2-test-v1")

    assert config["configurable"] == {"thread_id": str(INVESTIGATION)}  # the checkpoint key
    assert config["callbacks"] == [recorder]
    assert config["run_name"] == "investigation" and "investigation" in config["tags"]
    assert config["metadata"] == {"investigation_id": str(INVESTIGATION), "graph_version": "m2-test-v1"}
