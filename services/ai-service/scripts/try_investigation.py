"""Start an investigation through the real HTTP API and watch its SSE stream, end to end.

    uv run python scripts/try_investigation.py <incident_id> <tenant_id>

Needs Docker up (postgres, clickhouse, incident-service) and your .env. It boots the whole
FastAPI app in-process (so it also works on Windows), signs a short-lived ENGINEER token for
the tenant you pass, POSTs the investigation, prints every event as it arrives, then deletes
the investigation row (its events go with it) and its checkpoint thread.

WARNING: like scripts/run_graph.py, this sends a summary of that incident to your configured
triage model.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform == "win32":  # psycopg's async mode cannot use Windows' default event loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.graph.checkpoint import open_checkpointer
from app.main import create_app


def _token(secret: str, tenant_id: UUID) -> str:
    now = datetime.now(tz=UTC)
    claims = {
        "sub": str(uuid4()),
        "tenantId": str(tenant_id),
        "role": "ENGINEER",
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _clean_up(investigation_id: str) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:  # investigation_event rows are removed by ON DELETE CASCADE
        conn.execute(text("DELETE FROM ai.investigation WHERE id = :id"), {"id": investigation_id})
    engine.dispose()

    async def drop_thread() -> None:
        async with open_checkpointer(settings) as saver:
            await saver.adelete_thread(investigation_id)

    asyncio.run(drop_thread())


def main(incident_id: UUID, tenant_id: UUID) -> None:
    settings = get_settings()
    if not settings.investigation_graph_enabled:
        sys.exit("INVESTIGATION_GRAPH_ENABLED is false in your environment: nothing would run")
    assert settings.jwt_secret is not None, "JWT_SECRET is not set"
    headers = {"Authorization": f"Bearer {_token(settings.jwt_secret.get_secret_value(), tenant_id)}"}
    base = "/api/v1/investigations"

    investigation_id = None
    try:
        with TestClient(create_app()) as client:
            post = client.post(base, json={"incidentId": str(incident_id)}, headers=headers)
            print(f"POST {base} -> {post.status_code} {post.json()}")
            post.raise_for_status()
            investigation_id = post.json()["investigationId"]

            print("--- SSE stream ---")
            with client.stream("GET", f"{base}/{investigation_id}/stream", headers=headers) as stream:
                for line in stream.iter_lines():
                    if line.startswith(("event:", "data:")):
                        print(line[:230])

            final = client.get(f"{base}/{investigation_id}", headers=headers).json()
            print("--- final record ---")
            for key in ("status", "graphVersion", "toolCallCount", "error", "startedAt", "completedAt"):
                print(f"{key}: {final[key]}")
    finally:
        if investigation_id:
            _clean_up(investigation_id)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(UUID(sys.argv[1]), UUID(sys.argv[2]))
