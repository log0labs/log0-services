"""Run the graph once on a REAL incident and show where the time and tokens went, node by node.

    uv run python scripts/trace_run.py <incident_id> <tenant_id> [--prompts]

This is the local version of reading a LangSmith trace: no third party, nothing leaves your
machine except what the models already receive. It prints, in the order things happened:

* the time each graph node took,
* every LLM call: node, model, input and output tokens, seconds, prompt size,
* the totals (and the cost, if LLM_PRICES knows the model),
* with --prompts, the exact system and user message of every LLM call (it contains log
  excerpts: read it to decide what the redaction rules in M5.6 must hide).

WARNING: like run_graph.py, this sends the incident summary and log excerpts to your configured
models and runs the follow-up log searches against your log store. State is checkpointed to
Postgres under a throwaway thread id that is deleted at the end.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if sys.platform == "win32":  # psycopg's async mode cannot use Windows' default event loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from langchain_core.messages import BaseMessage

from app.config import get_settings
from app.graph.builder import GRAPH_VERSION, build_graph
from app.graph.checkpoint import open_checkpointer
from app.graph.state import InvestigationContext, new_state
from app.graph.usage import UsageRecorder, run_config
from app.integrations.clickhouse import LogStore
from app.integrations.incident_db import IncidentReader
from app.integrations.incident_service import IncidentApiClient
from app.llm.models import PurposeChatModels
from app.schemas.investigation import InvestigationScope


class PromptCapture(UsageRecorder):
    """The recorder plus the prompt text of every call. Only this script keeps prompts."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.prompts: list[tuple[str, str]] = []

    async def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[BaseMessage]], **kwargs: Any) -> None:
        await super().on_chat_model_start(serialized, messages, **kwargs)
        node = (kwargs.get("metadata") or {}).get("langgraph_node") or "?"
        text = "\n\n".join(f"[{m.type}]\n{m.content}" for batch in messages for m in batch)
        self.prompts.append((str(node), text))


async def main(incident_id: UUID, tenant_id: UUID, show_prompts: bool) -> None:
    settings = get_settings()
    logs, history = LogStore(settings), IncidentReader(settings)
    thread_id = uuid4()
    recorder = PromptCapture(settings.llm_prices)
    try:
        async with open_checkpointer(settings) as saver:
            graph = build_graph(saver)
            context = InvestigationContext(
                tenant_id=tenant_id,
                logs=logs,
                incidents=IncidentApiClient(settings),
                history=history,
                models=PurposeChatModels(settings),
            )
            state = new_state(investigation_id=str(thread_id), incident_id=str(incident_id), scope=InvestigationScope())
            final = await graph.ainvoke(state, run_config(thread_id, recorder, GRAPH_VERSION), context=context)
            await saver.adelete_thread(str(thread_id))
    finally:
        await logs.close()
        await history.close()

    print(f"status: {final['status'].value} | error: {final['error']} | tool_calls: {final['tool_calls']} | verify_rounds: {final['verify_round']}")
    print(f"root cause [{final['confidence_band']}]: {final['root_cause'].statement[:160] if final['root_cause'] else None}")

    print("\nnodes, in the order they finished")
    for node in recorder.nodes:
        print(f"  {node.node:18} {node.seconds:7.2f} s")

    print("\nLLM calls")
    print(f"  {'node':14} {'model':26} {'in tok':>7} {'out tok':>7} {'secs':>6} {'prompt chars':>12}  ok")
    for call in recorder.calls:
        print(
            f"  {call.node:14} {call.model:26} {call.input_tokens:7} {call.output_tokens:7} "
            f"{call.seconds:6.2f} {call.prompt_chars:12}  {'yes' if call.ok else 'FAILED'}"
        )
    cost = recorder.cost_usd()
    print(
        f"\ntotal: {recorder.input_tokens} input + {recorder.output_tokens} output tokens, "
        f"cost {cost if cost is not None else 'unknown (set LLM_PRICES for the models above)'}"
    )

    if show_prompts:
        for number, (node, text) in enumerate(recorder.prompts, start=1):
            print(f"\n{'=' * 100}\nCALL {number} from node {node}\n{'=' * 100}\n{text}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit(__doc__)
    asyncio.run(main(UUID(args[0]), UUID(args[1]), "--prompts" in sys.argv))
