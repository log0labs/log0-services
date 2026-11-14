"""Shared pieces for the runbook tests: an app on the real Postgres with a counting embedder, and cleanup."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.runbooks import get_embedder
from app.config import get_settings
from app.main import create_app
from app.retrieval.embeddings import EmbeddingError, HashingEmbedder
from tests.jwt_helpers import TENANT_A, TENANT_B, mint_access_token


class CountingEmbedder(HashingEmbedder):
    """The built-in embedder, but it counts calls and can be made to fail."""

    def __init__(self, model_id: str = "hash-384-v1") -> None:
        self.model_id = model_id
        self.document_calls = 0
        self.fail = False

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        if self.fail:
            raise EmbeddingError("simulated outage")
        return await super().embed_documents(texts)


@dataclass
class RunbookEnv:
    client: TestClient
    embedder: CountingEmbedder

    def headers(self, *, tenant=TENANT_A, role: str = "ADMIN") -> dict[str, str]:
        secret = get_settings().jwt_secret.get_secret_value()
        return {"Authorization": f"Bearer {mint_access_token(secret, tenant_id=tenant, role=role)}"}

    def upload(self, title: str, markdown: str, **headers):
        return self.client.post(
            "/api/v1/runbooks", json={"title": title, "markdown": markdown}, headers=self.headers(**headers)
        )


def wipe_runbooks() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        for tenant in (TENANT_A, TENANT_B):
            conn.execute(text("DELETE FROM ai.runbook WHERE tenant_id = :t"), {"t": tenant})  # chunks cascade
    engine.dispose()


@contextmanager
def make_runbook_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunbookEnv]:
    get_settings.cache_clear()
    wipe_runbooks()
    app = create_app()
    embedder = CountingEmbedder()
    app.dependency_overrides[get_embedder] = lambda: embedder
    try:
        with TestClient(app) as client:
            yield RunbookEnv(client, embedder)
    finally:
        wipe_runbooks()
