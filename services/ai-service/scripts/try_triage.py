"""Run the triage node once against the REAL triage model (uses .env; sends invented data).

    uv run python scripts/try_triage.py

Nothing is read from log0's databases: the incident below is made up, so no real log data
leaves your machine. It shows what the model decides and what normalize_triage makes of it.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graph.nodes.load_context import incident_evidence, stack_trace_evidence
from app.graph.nodes.triage import triage
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.llm.models import PurposeChatModels
from app.parsing.stacktrace import parse_stack_trace
from app.schemas.investigation import InvestigationScope
from app.tools.incidents import IncidentContext

TRACE = (
    "java.net.SocketTimeoutException: Connection timeout after 30000ms calling payment gateway\n"
    "\tat com.log0.payment.PaymentProcessor.charge(PaymentProcessor.java:120)\n"
    "\tat com.log0.common.Dispatcher.run(Dispatcher.java:88)\n"
)


async def main() -> None:
    settings = get_settings()
    first = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    incident = IncidentContext(
        incident_id=uuid4(),
        fingerprint="f" * 64,
        service_name="payment-service",
        environment="production",
        severity="HIGH",
        status="NEW",
        occurrence_count=1500,
        first_seen_at=first,
        last_seen_at=first + timedelta(minutes=10),
        resolved_at=None,
        top_messages=["Connection timeout after 30000ms"],
        ai_summary=None,
    )
    parsed = parse_stack_trace(TRACE)
    state = new_state(investigation_id="try", incident_id=str(incident.incident_id), scope=InvestigationScope())
    state["incident"] = incident
    state["stack_frames"] = parsed.frames
    state["evidence"] = [incident_evidence(incident), stack_trace_evidence(incident.fingerprint, TRACE, parsed)]

    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("triage", triage)
    builder.add_edge(START, "triage")
    builder.add_edge("triage", END)
    ctx = InvestigationContext(
        tenant_id=uuid4(),
        logs=SimpleNamespace(),  # type: ignore[arg-type]
        incidents=SimpleNamespace(),  # type: ignore[arg-type]
        history=SimpleNamespace(),  # type: ignore[arg-type]
        models=PurposeChatModels(settings),
    )
    print("triage model:", settings.llm_triage.provider, settings.llm_triage.model)
    final = await builder.compile().ainvoke(state, context=ctx)
    print(final["triage"].model_dump_json(indent=2))
    print("gaps:", final["gaps"])


if __name__ == "__main__":
    asyncio.run(main())
