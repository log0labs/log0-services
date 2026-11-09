"""Shared pieces for the settings / repository API tests: fakes, a ready client, cleanup.

The app is the real one with the real Postgres; only GitHub and the model provider are faked
(through ``app.dependency_overrides``), so no test touches the network.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.dependencies import get_connection_checker, get_github
from app.config import get_settings
from app.integrations.github import GitHubRepository
from app.llm.connection_check import ConnectionCheck
from app.main import create_app
from app.security.crypto import SecretBox
from tests.jwt_helpers import TENANT_A, TENANT_B, mint_access_token

MASTER_KEY = Fernet.generate_key().decode()
TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
API_KEY = "gsk_live_0123456789abcdefABCD"


class FakeGitHub:
    """Answers like GitHub would; records what it was asked, including the token it received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.result = GitHubRepository(owner="Acme", name="Shop", default_branch="trunk", private=True)
        self.error: Exception | None = None

    async def get_repository(self, owner: str, name: str, token: str) -> GitHubRepository:
        self.calls.append((owner, name, token))
        if self.error is not None:
            raise self.error
        return self.result


class FakeChecker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.result = ConnectionCheck(ok=True, latency_ms=42)

    async def __call__(self, provider: str, model: str, api_key: str) -> ConnectionCheck:
        self.calls.append((provider, model, api_key))
        return self.result


@dataclass
class Env:
    client: TestClient
    github: FakeGitHub
    checker: FakeChecker
    box: SecretBox | None
    app: object = field(repr=False, default=None)

    def headers(self, *, tenant=TENANT_A, role: str = "ADMIN") -> dict[str, str]:
        secret = get_settings().jwt_secret.get_secret_value()
        return {"Authorization": f"Bearer {mint_access_token(secret, tenant_id=tenant, role=role)}"}


def wipe_tenants() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        for tenant in (TENANT_A, TENANT_B):
            for table in ("service_repository", "repository_connection", "provider_config"):
                conn.execute(text(f"DELETE FROM ai.{table} WHERE tenant_id = :t"), {"t": tenant})
    engine.dispose()


@contextmanager
def make_env(monkeypatch: pytest.MonkeyPatch, *, with_master_key: bool = True) -> Iterator[Env]:
    if with_master_key:
        monkeypatch.setenv("SECRETS_KEYS", f'{{"k1": "{MASTER_KEY}"}}')
        monkeypatch.setenv("SECRETS_ACTIVE_KEY_ID", "k1")
    else:
        monkeypatch.delenv("SECRETS_KEYS", raising=False)
        monkeypatch.delenv("SECRETS_ACTIVE_KEY_ID", raising=False)
    get_settings.cache_clear()
    wipe_tenants()
    app = create_app()
    github, checker = FakeGitHub(), FakeChecker()
    app.dependency_overrides[get_github] = lambda: github
    app.dependency_overrides[get_connection_checker] = lambda: checker
    try:
        with TestClient(app) as client:
            yield Env(client, github, checker, SecretBox({"k1": MASTER_KEY}, "k1") if with_master_key else None, app)
    finally:
        wipe_tenants()
