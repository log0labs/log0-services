"""Step 5.2: the runbook endpoints, with the real app and the real Postgres (pgvector)."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.runbooks import MAX_CHUNKS, MAX_MARKDOWN_CHARS
from app.config import get_settings
from app.db.session import create_session_factory
from app.retrieval.embeddings import HashingEmbedder
from app.store import runbooks as store
from tests.conftest import requires_postgres
from tests.jwt_helpers import TENANT_A, TENANT_B
from tests.runbook_support import RunbookEnv, make_runbook_env

pytestmark = requires_postgres

POOL = """# Database

## Connection pool

When the pool is exhausted, raise the pool size and look for leaked connections.

## Slow queries

Run EXPLAIN on the slowest statement.

# Cache

Flush the cache after a deploy.
"""


@pytest.fixture
def env(monkeypatch) -> RunbookEnv:
    with make_runbook_env(monkeypatch) as e:
        yield e


def count(sql: str, **params) -> int:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


# ----------------------------------------------------------------------------- upload


def test_admin_uploads_a_runbook_and_it_is_split_and_stored(env):
    response = env.upload("Database playbook", POOL)
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Database playbook"
    assert body["chunkCount"] == 3
    assert body["embeddingModel"] == "hash-384-v1"
    assert count("SELECT count(*) FROM ai.runbook_chunk WHERE runbook_id = :id", id=body["id"]) == 3
    paths = [
        r[0]
        for r in create_engine(get_settings().database_url)
        .connect()
        .execute(text("SELECT heading_path FROM ai.runbook_chunk WHERE runbook_id = :id ORDER BY ordinal"), {"id": body["id"]})
    ]
    assert paths == ["Database > Connection pool", "Database > Slow queries", "Cache"]


def test_the_same_text_again_changes_nothing_and_does_not_embed_again(env):
    first = env.upload("Playbook", POOL)
    calls = env.embedder.document_calls
    second = env.upload("Playbook", POOL)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert env.embedder.document_calls == calls


def test_new_text_under_the_same_title_replaces_the_old_chunks(env):
    first = env.upload("Playbook", POOL).json()
    second = env.upload("Playbook", "# Only\n\nnew text").json()
    assert second["id"] != first["id"]
    assert second["chunkCount"] == 1
    assert count("SELECT count(*) FROM ai.runbook WHERE tenant_id = :t", t=TENANT_A) == 1
    assert count("SELECT count(*) FROM ai.runbook_chunk WHERE tenant_id = :t", t=TENANT_A) == 1  # old chunks gone


def test_a_changed_embedding_model_re_indexes_the_same_text(env):
    env.upload("Playbook", POOL)
    env.embedder.model_id = "another-model"
    calls = env.embedder.document_calls
    response = env.upload("Playbook", POOL)
    assert response.status_code == 201
    assert response.json()["embeddingModel"] == "another-model"
    assert env.embedder.document_calls == calls + 1


def test_only_an_admin_can_upload(env):
    for role in ("ENGINEER", "VIEWER"):
        assert env.upload("x", "text", role=role).status_code == 403
    assert env.client.post("/api/v1/runbooks", json={"title": "x", "markdown": "y"}).status_code == 401
    assert count("SELECT count(*) FROM ai.runbook") == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "markdown": "text"},
        {"title": "  ", "markdown": "text"},
        {"title": "a\nb", "markdown": "text"},
        {"title": "t" * 201, "markdown": "text"},
        {"title": "t", "markdown": ""},
        {"title": "t", "markdown": "x" * (MAX_MARKDOWN_CHARS + 1)},
        {"title": "t"},
    ],
)
def test_bad_requests_are_refused(env, payload):
    assert env.client.post("/api/v1/runbooks", json=payload, headers=env.headers()).status_code == 422


def test_a_runbook_with_no_text_is_refused(env):
    response = env.upload("Empty", "# Just a heading\n")
    assert response.status_code == 422
    assert "no text" in response.json()["detail"]


def test_a_runbook_that_is_too_long_is_refused_with_advice(env):
    big = "\n\n".join(f"## Part {i}\n\ntext {i}" for i in range(MAX_CHUNKS + 1))
    response = env.upload("Huge", big)
    assert response.status_code == 422
    assert "split it" in response.json()["detail"]
    assert env.embedder.document_calls == 0  # refused before embedding anything


def test_an_embedding_outage_is_a_502_and_stores_nothing(env):
    env.embedder.fail = True
    response = env.upload("Playbook", POOL)
    assert response.status_code == 502
    assert "simulated outage" not in response.text
    assert count("SELECT count(*) FROM ai.runbook") == 0


def test_a_failed_replacement_keeps_the_old_runbook(env):
    env.upload("Playbook", POOL)
    env.embedder.fail = True
    assert env.upload("Playbook", "# New\n\ndifferent text").status_code == 502
    assert count("SELECT chunk_count FROM ai.runbook WHERE tenant_id = :t", t=TENANT_A) == 3


# ----------------------------------------------------------------------- list, delete


def test_everyone_can_list_but_only_their_own_tenants_runbooks(env):
    env.upload("Alpha", "# A\n\ntext a")
    env.upload("Beta", "# B\n\ntext b", tenant=TENANT_B)
    mine = env.client.get("/api/v1/runbooks", headers=env.headers(role="VIEWER")).json()
    assert [r["title"] for r in mine] == ["Alpha"]
    theirs = env.client.get("/api/v1/runbooks", headers=env.headers(tenant=TENANT_B)).json()
    assert [r["title"] for r in theirs] == ["Beta"]
    assert env.client.get("/api/v1/runbooks").status_code == 401


def test_two_tenants_can_use_the_same_title(env):
    assert env.upload("Shared title", "# A\n\ntext").status_code == 201
    assert env.upload("Shared title", "# A\n\ntext", tenant=TENANT_B).status_code == 201
    assert count("SELECT count(*) FROM ai.runbook") == 2


def test_delete_removes_the_runbook_and_its_chunks(env):
    rid = env.upload("Playbook", POOL).json()["id"]
    assert env.client.delete(f"/api/v1/runbooks/{rid}", headers=env.headers()).status_code == 204
    assert count("SELECT count(*) FROM ai.runbook_chunk") == 0
    assert env.client.delete(f"/api/v1/runbooks/{rid}", headers=env.headers()).status_code == 404


def test_delete_needs_admin_and_cannot_reach_another_tenant(env):
    rid = env.upload("Playbook", POOL).json()["id"]
    assert env.client.delete(f"/api/v1/runbooks/{rid}", headers=env.headers(role="ENGINEER")).status_code == 403
    assert env.client.delete(f"/api/v1/runbooks/{rid}", headers=env.headers(tenant=TENANT_B)).status_code == 404
    assert env.client.delete(f"/api/v1/runbooks/{uuid4()}", headers=env.headers()).status_code == 404
    assert count("SELECT count(*) FROM ai.runbook") == 1


# ------------------------------------------------------------------------------ search


def run(coro):
    return asyncio.run(coro)


async def _search(tenant, query: str, *, model="hash-384-v1", limit=5):
    engine = create_async_engine(get_settings().database_url_async)
    try:
        async with create_session_factory(engine)() as session:
            vector = await HashingEmbedder().embed_query(query)
            return await store.search(session, tenant_id=tenant, query_vector=vector, limit=limit, embedding_model=model)
    finally:
        await engine.dispose()


def test_search_finds_the_nearest_chunk_first(env):
    env.upload("Playbook", POOL)
    hits = run(_search(TENANT_A, "the connection pool is exhausted"))
    assert hits[0].heading_path == "Database > Connection pool"
    assert hits[0].runbook_title == "Playbook"
    assert hits[0].distance < hits[-1].distance
    assert len(hits[0].embedding) == 384


def test_search_never_returns_another_tenants_chunks(env):
    env.upload("Playbook", POOL)
    assert run(_search(TENANT_B, "connection pool exhausted")) == []
    env.upload("Their playbook", "# Pool\n\nconnection pool exhausted, restart", tenant=TENANT_B)
    titles = {h.runbook_title for h in run(_search(TENANT_A, "connection pool exhausted"))}
    assert titles == {"Playbook"}


def test_search_ignores_runbooks_made_with_another_model(env):
    env.upload("Playbook", POOL)
    assert run(_search(TENANT_A, "connection pool", model="some-other-model")) == []


def test_search_respects_the_limit(env):
    env.upload("Playbook", POOL)
    assert len(run(_search(TENANT_A, "pool", limit=2))) == 2
