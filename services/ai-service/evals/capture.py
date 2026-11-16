"""Record a fixture from a live log0 stack: one incident, and exactly what the tools read for it.

    uv run python -m evals.capture --tenant <tenant-uuid> --service payments \
        --contains "nil map" --out evals/fixtures/bug-04-nil-map

Needs the docker stack up (ClickHouse, Postgres, incident-service) and this service's .env. It finds the
incident (by service and a piece of its top message, or by ``--incident-id``), runs the real ``load_context``,
``gather_logs`` and ``gather_history`` nodes against the real backends wrapped in recorders, and writes
``incident.json``, ``logs.json`` and ``history.json``. No language model is called, and nothing leaves the machine.
Fixtures are committed; this script is only needed to make new ones.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

import httpx
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graph.nodes.gather_history import gather_history
from app.graph.nodes.gather_logs import gather_logs
from app.graph.nodes.load_context import load_context
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.integrations.clickhouse import LogStore
from app.integrations.incident_db import IncidentReader
from app.integrations.incident_service import IncidentApiClient
from app.schemas.investigation import InvestigationScope
from evals.fixtures import (
    RecordingHistory,
    RecordingIncidents,
    RecordingLogs,
    save_fixture,
)

if sys.platform == "win32":  # psycopg's async mode cannot use Windows' default event loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def find_incident(base_url: str, tenant: UUID, service: str, contains: str) -> UUID:
    response = httpx.get(f"{base_url}/api/v1/incidents", params={"tenantId": str(tenant), "size": 100}, timeout=20)
    response.raise_for_status()
    body = response.json()
    items = body.get("content", body) if isinstance(body, dict) else body
    matches = [
        i for i in items if i["serviceName"] == service and any(contains.lower() in m.lower() for m in i.get("topMessages") or [])
    ]
    if not matches:
        raise SystemExit(f"no incident of {service} with {contains!r} in its top messages for tenant {tenant}")
    return UUID(max(matches, key=lambda i: i["occurrenceCount"])["incidentId"])


async def capture(tenant: UUID, incident_id: UUID, out: Path) -> None:
    settings = get_settings()
    log_store, reader = LogStore(settings), IncidentReader(settings)
    logs, history, incidents = RecordingLogs(log_store), RecordingHistory(reader), RecordingIncidents(IncidentApiClient(settings))
    try:
        builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
        for name, node in (("load_context", load_context), ("gather_logs", gather_logs), ("gather_history", gather_history)):
            builder.add_node(name, node)
        builder.add_edge(START, "load_context")
        builder.add_edge("load_context", "gather_logs")
        builder.add_edge("gather_logs", "gather_history")
        builder.add_edge("gather_history", END)
        context = InvestigationContext(tenant_id=tenant, logs=logs, incidents=incidents, history=history)
        state = new_state(investigation_id="capture", incident_id=str(incident_id), scope=InvestigationScope())
        final = await builder.compile().ainvoke(state, context=context)
        if final["incident"] is None:
            raise SystemExit("the incident could not be read: " + "; ".join(final["gaps"]))
        assert incidents.payload is not None
        save_fixture(out, incident=incidents.payload, logs=logs.rows, history=history.rows)
        print(f"captured {out}: kinds={sorted(logs.rows)} history_rows={len(history.rows)} evidence={len(final['evidence'])}")
    finally:
        await log_store.close()
        await reader.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True, type=UUID)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--incident-id", type=UUID)
    parser.add_argument("--service")
    parser.add_argument("--contains", default="")
    args = parser.parse_args()
    if args.incident_id is None:
        if not args.service:
            raise SystemExit("give --incident-id, or --service and --contains")
        args.incident_id = find_incident(get_settings().incident_service_url, args.tenant, args.service, args.contains)
    asyncio.run(capture(args.tenant, args.incident_id, args.out))


if __name__ == "__main__":
    main()
