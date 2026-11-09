"""Measure one investigation: tokens, cost and where the time went.

LangChain reports everything that happens in a run to *callback handlers*. This one listens
to the two things worth knowing and keeps them in memory:

* every **LLM call**: which graph node made it, which model answered, input and output
  tokens (the provider's own count, from the message's ``usage_metadata``), how long it took,
  and how big the prompt was in characters;
* every **node**: how long it ran.

It works without LangSmith or any third party: the executor hands one recorder to the graph
run, the graph passes it down to every model call inside every node, and afterwards the
executor stores the totals on the investigation row and logs the breakdown. Prompt text is
never kept, only its length.

Cost is computed only for models whose price you configured (``LLM_PRICES``, USD per million
tokens). A price I did not get from you would be a guess presented as a number, so an unknown
model leaves the cost empty rather than wrong.
"""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

Prices = Mapping[str, tuple[float, float]]  # model name -> (USD per 1M input tokens, USD per 1M output tokens)
UNKNOWN_MODEL = "unknown"


@dataclass(frozen=True)
class LlmCall:
    node: str
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float
    prompt_chars: int
    ok: bool


@dataclass(frozen=True)
class NodeTiming:
    node: str
    seconds: float


def _prompt_chars(messages: list[list[BaseMessage]]) -> int:
    return sum(len(str(m.content)) for batch in messages for m in batch)


class UsageRecorder(AsyncCallbackHandler):
    """Collects the calls and node timings of one run. Create one per investigation."""
    
    raise_error = False # a measuring tool must never break the run it measures
    
    def __init__(self, prices: Prices | None = None) -> None:
        self._prices: Prices = prices or {}
        self.calls: list[LlmCall] = []
        self.nodes: list[NodeTiming] = []
        self._llm_started: dict[UUID, tuple[float, str, int]] = {}
        self._node_started: dict[UUID, tuple[float, str]] = {}
    
    # ------------------------------------------------------------------ LLM calls
    
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node = (metadata or {}).get("langgraph_node") or "(outside a node)"
        self._llm_started[run_id] = (time.monotonic(), str(node), _prompt_chars(messages))

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        started, node, chars = self._llm_started.pop(run_id, (time.monotonic(), "(unknown)", 0))
        message = response.generations[0][0].message if response.generations and response.generations[0] else None
        usage = getattr(message, "usage_metadata", None) or {}
        metadata = getattr(message, "response_metadata", None) or {}
        self.calls.append(
            LlmCall(
                node=node,
                model=str(metadata.get("model_name") or metadata.get("model") or UNKNOWN_MODEL),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                seconds=time.monotonic() - started,
                prompt_chars=chars,
                ok=True,
            )
        )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        started, node, chars = self._llm_started.pop(run_id, (time.monotonic(), "(unknown)", 0))
        self.calls.append(LlmCall(node, UNKNOWN_MODEL, 0, 0, time.monotonic() - started, chars, ok=False))

    # ----------------------------------------------------------------------- nodes

    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        node = (metadata or {}).get("langgraph_node")
        if node and name == node:  # the run that IS the node, not a helper chain inside it
            self._node_started[run_id] = (time.monotonic(), node)

    async def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish_node(run_id)

    async def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish_node(run_id)

    def _finish_node(self, run_id: UUID) -> None:
        started = self._node_started.pop(run_id, None)
        if started:
            self.nodes.append(NodeTiming(started[1], time.monotonic() - started[0]))

    # ---------------------------------------------------------------------- totals

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls if c.ok)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls if c.ok)

    @property
    def has_usage(self) -> bool:
        """True when at least one call reported tokens (so zero totals mean 'nothing known')."""
        return any(c.ok and (c.input_tokens or c.output_tokens) for c in self.calls)

    def cost_usd(self) -> Decimal | None:
        """Total cost, or ``None`` when any model that answered has no configured price."""
        answered = [c for c in self.calls if c.ok]
        if not answered:
            return None
        total = Decimal(0)
        for call in answered:
            price = self._prices.get(call.model)
            if price is None:
                return None
            total += (
                Decimal(call.input_tokens) * Decimal(str(price[0]))
                + Decimal(call.output_tokens) * Decimal(str(price[1]))
            ) / Decimal(1_000_000)
        return total.quantize(Decimal("0.000001"))

    def log_summary(self, investigation_id: UUID) -> None:
        logger.info(
            "investigation %s usage: %d LLM calls (%d failed), %d input + %d output tokens, cost %s",
            investigation_id,
            len(self.calls),
            sum(1 for c in self.calls if not c.ok),
            self.input_tokens,
            self.output_tokens,
            self.cost_usd() if self.cost_usd() is not None else "unknown (no price configured)",
        )


def run_config(investigation_id: UUID, recorder: UsageRecorder, graph_version: str) -> dict[str, Any]:
    """The LangChain run config for one investigation.

    ``thread_id`` is the checkpoint key (one per investigation). The name, tags and metadata
    make the run easy to find and read in LangSmith when tracing is on; they carry no tenant
    or incident data, only the investigation id and the graph version.
    """
    return {
        "configurable": {"thread_id": str(investigation_id)},
        "callbacks": [recorder],
        "run_name": "investigation",
        "tags": ["log0", "investigation"],
        "metadata": {"investigation_id": str(investigation_id), "graph_version": graph_version},
    }
