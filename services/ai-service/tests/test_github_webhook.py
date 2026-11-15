"""``POST /api/v1/github/webhook``: signature check and what each event does (real Postgres)."""

import hashlib
import hmac
import json
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from app.api.dependencies import get_github_app
from app.config import get_settings
from tests.config_api_support import Env, make_env
from tests.jwt_helpers import TENANT_A, USER_ID
from tests.test_github_app_api import INSTALLATION, FakeAppClient

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

URL = "/api/v1/github/webhook"
SECRET = "webhook-secret-for-tests"
COUNT_INSTALLATIONS = "SELECT count(*) FROM ai.github_installation WHERE installation_id = :i"
REPOSITORY_COLUMN = "SELECT {column} FROM ai.repository_connection WHERE tenant_id = :t"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Env, FakeAppClient]]:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    with make_env(monkeypatch) as e:
        fake = FakeAppClient()
        e.app.dependency_overrides[get_github_app] = lambda: fake  # type: ignore[attr-defined]
        _seed()
        yield e, fake


def _seed() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai.github_installation (tenant_id, installation_id, account_login, account_type, created_by) "
                "VALUES (:t, :i, 'acme', 'Organization', :u)"
            ),
            {"t": TENANT_A, "i": INSTALLATION, "u": USER_ID},
        )
        conn.execute(
            text(
                "INSERT INTO ai.repository_connection (tenant_id, owner, name, token_ciphertext, key_id, created_by, "
                "installation_id, github_repository_id, pr_writes_enabled) "
                "VALUES (:t, 'acme', 'shop', 'x', 'k1', :u, :i, 7, true)"
            ),
            {"t": TENANT_A, "i": INSTALLATION, "u": USER_ID},
        )
    engine.dispose()


def _scalar(sql: str):
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        value = conn.execute(text(sql), {"i": INSTALLATION, "t": TENANT_A}).scalar()
    engine.dispose()
    return value


def _repository(column: str):
    return _scalar(REPOSITORY_COLUMN.format(column=column))


def _post(e: Env, event: str, payload: dict, *, secret: str = SECRET, signature: str | None = None):
    body = json.dumps(payload).encode()
    sig = signature if signature is not None else "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": sig, "Content-Type": "application/json"}
    return e.client.post(URL, content=body, headers=headers)


def _installation_payload(action: str) -> dict:
    return {"action": action, "installation": {"id": INSTALLATION}}


@pytest.mark.parametrize("signature", ["", "sha256=" + "0" * 64, "nope"])
def test_a_missing_or_wrong_signature_is_refused_and_changes_nothing(env: tuple[Env, FakeAppClient], signature: str) -> None:
    e, _ = env

    response = _post(e, "installation", _installation_payload("deleted"), signature=signature)

    assert response.status_code == 401
    assert _scalar(COUNT_INSTALLATIONS) == 1


def test_a_signature_made_with_another_secret_is_refused(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env

    assert _post(e, "installation", _installation_payload("deleted"), secret="other").status_code == 401


def test_without_a_configured_secret_webhooks_are_off(env: tuple[Env, FakeAppClient], monkeypatch: pytest.MonkeyPatch) -> None:
    e, _ = env
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    get_settings.cache_clear()
    e.app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update={"github_webhook_secret": None})  # type: ignore[attr-defined]

    assert _post(e, "ping", {}).status_code == 503


def test_uninstalling_removes_the_installation_and_forgets_tokens(env: tuple[Env, FakeAppClient]) -> None:
    e, fake = env

    response = _post(e, "installation", _installation_payload("deleted"))

    assert response.status_code == 202 and response.json() == {"handled": True}
    assert _scalar(COUNT_INSTALLATIONS) == 0
    assert _repository("installation_id") is None
    assert fake.auth.forgotten == [INSTALLATION]


def test_a_redelivered_event_is_harmless(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env

    assert _post(e, "installation", _installation_payload("deleted")).status_code == 202
    assert _post(e, "installation", _installation_payload("deleted")).status_code == 202


def test_suspend_and_unsuspend_toggle_the_installation(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    query = "SELECT suspended_at IS NOT NULL FROM ai.github_installation WHERE installation_id = :i"

    _post(e, "installation", _installation_payload("suspend"))
    suspended = _scalar(query)
    _post(e, "installation", _installation_payload("unsuspend"))
    resumed = not _scalar(query)

    assert (suspended, resumed) == (True, True)


def test_a_repository_removed_from_the_installation_loses_write_access(env: tuple[Env, FakeAppClient]) -> None:
    e, fake = env
    payload = {"action": "removed", "installation": {"id": INSTALLATION}, "repositories_removed": [{"id": 7}]}

    assert _post(e, "installation_repositories", payload).status_code == 202

    assert _repository("pr_writes_enabled") is False
    assert fake.auth.forgotten == [INSTALLATION]


def test_a_repository_added_changes_nothing_else(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    payload = {"action": "added", "installation": {"id": INSTALLATION}, "repositories_added": [{"id": 9}]}

    _post(e, "installation_repositories", payload)

    assert _repository("pr_writes_enabled") is True


def test_unknown_events_and_unknown_installations_are_accepted_and_ignored(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env

    assert _post(e, "ping", {"zen": "hi"}).json() == {"handled": False}
    assert _post(e, "installation", {"action": "deleted", "installation": {"id": 999}}).status_code == 202
    assert _scalar(COUNT_INSTALLATIONS) == 1


def test_an_unreadable_payload_is_a_client_error(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env

    assert _post(e, "installation", {"action": "deleted"}).status_code == 400
