"""Run the whole investigation graph once on a REAL incident and print what it found.

    uv run python scripts/run_graph.py <incident_id> <tenant_id>

Needs Docker up (postgres, clickhouse, incident-service), your .env loaded, and a real
incident id. Pick one with:

    docker exec log0-postgres psql -U log0 -d log0 -tAc "select incident_id, tenant_id from public.incident limit 3"

WARNING: this sends a summary of that incident (messages, stack trace summary) to your
configured triage and investigation models, together with log excerpts, and the follow-up
searches verify asks for are run against your log store. State is checkpointed
to Postgres under a throwaway thread id that is deleted at the end.
"""

import asyncio
import sys
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Model text can contain characters a Windows console cannot encode (e.g. a non-breaking hyphen)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if sys.platform == "win32":  # psycopg's async mode cannot use Windows' default event loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import get_settings
from app.graph.builder import build_graph
from app.graph.checkpoint import open_checkpointer
from app.graph.state import InvestigationContext, new_state
from app.integrations.clickhouse import LogStore
from app.integrations.incident_db import IncidentReader
from app.integrations.incident_service import IncidentApiClient
from app.llm.models import PurposeChatModels
from app.schemas.investigation import InvestigationScope


async def main(incident_id: UUID, tenant_id: UUID) -> None:
    settings = get_settings()
    logs, history = LogStore(settings), IncidentReader(settings)
    thread_id = str(uuid4())
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
            state = new_state(
                investigation_id=thread_id, incident_id=str(incident_id), scope=InvestigationScope()
            )
            final = await graph.ainvoke(state, {"configurable": {"thread_id": thread_id}}, context=context)
            steps = [s async for s in graph.aget_state_history({"configurable": {"thread_id": thread_id}})]
            await saver.adelete_thread(thread_id)
    finally:
        await logs.close()
        await history.close()

    print(f"status: {final['status'].value} | error: {final['error']} | tool_calls: {final['tool_calls']} | verify_rounds: {final['verify_round']} | checkpoints: {len(steps)}")
    triage = final["triage"]
    if triage:
        print(f"triage: {triage.category} -> {triage.gather}\n  {triage.summary}")
        for h in triage.initial_hypotheses:
            print(f"  hypothesis: {h}")
    print(f"gaps: {final['gaps']}")
    for h in final["hypotheses"]:
        print(f"  {h.id} [{h.status}] {h.statement}")
        print(f"      supports: {h.supporting_evidence}  contradicts: {h.contradicting_evidence}")
    cause = final["root_cause"]
    if cause:
        print(f"root cause [{final['confidence_band']}] (hypothesis {cause.hypothesis_id}): {cause.statement}")
        print(f"  cites: {cause.evidence_ids}")
        for question in cause.unresolved_questions:
            print(f"  unresolved: {question}")
    advice = final["recommendation"]
    if advice:
        print(f"recommendation: {advice.summary}")
        for step in advice.verification_steps:
            print(f"  verify: {step}")
    for item in final["evidence"]:
        print(f"  [{item.kind:8}] {item.id}  {item.summary[:150]}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(UUID(sys.argv[1]), UUID(sys.argv[2])))
