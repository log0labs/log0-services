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
        _delete_investigations()


def _delete_investigations() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ai.investigation WHERE tenant_id = :t"), {"t": TENANT_A})
    engine.dispose()


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


# ------------------------------------------------------------- pull request state


def _add_pull_request(number: int = 5) -> str:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        investigation_id = conn.execute(
            text("INSERT INTO ai.investigation (tenant_id, incident_id, requested_by, status) VALUES (:t, gen_random_uuid(), :u, 'COMPLETED') RETURNING id"),
            {"t": TENANT_A, "u": USER_ID},
        ).scalar_one()
        repository_id = conn.execute(text("SELECT id FROM ai.repository_connection WHERE tenant_id = :t"), {"t": TENANT_A}).scalar_one()
        conn.execute(
            text(
                "INSERT INTO ai.pull_request (tenant_id, investigation_id, repository_id, branch, base_sha, number, state, approved_by) "
                "VALUES (:t, :i, :r, 'log0/fix-a-b', 'a', :n, 'open', :u)"
            ),
            {"t": TENANT_A, "i": investigation_id, "r": repository_id, "n": number, "u": USER_ID},
        )
    engine.dispose()
    return str(investigation_id)


def _pr_payload(action: str, *, number: int = 5, merged: bool = False, repository: int = 7) -> dict:
    return {
        "action": action,
        "installation": {"id": INSTALLATION},
        "repository": {"id": repository},
        "pull_request": {"number": number, "merged": merged},
    }


def _state_and_events(investigation_id: str) -> tuple[str, list[str]]:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        state = conn.execute(text("SELECT state FROM ai.pull_request WHERE investigation_id = :i"), {"i": investigation_id}).scalar_one()
        events = [r[0] for r in conn.execute(text("SELECT type FROM ai.investigation_event WHERE investigation_id = :i ORDER BY seq"), {"i": investigation_id})]
    engine.dispose()
    return state, events


@pytest.mark.parametrize(
    ("action", "merged", "state"),
    [("closed", True, "merged"), ("closed", False, "closed"), ("reopened", False, "open")],
)
def test_a_pull_request_merged_closed_or_reopened_on_github_is_recorded(env: tuple[Env, FakeAppClient], action: str, merged: bool, state: str) -> None:
    e, _ = env
    investigation_id = _add_pull_request()

    response = _post(e, "pull_request", _pr_payload(action, merged=merged))

    assert response.status_code == 202 and response.json() == {"handled": True}
    assert _state_and_events(investigation_id) == (state, [f"pr.{state}"])


def test_events_for_other_pull_requests_or_repositories_change_nothing(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    investigation_id = _add_pull_request(number=5)

    for payload in (_pr_payload("closed", number=6), _pr_payload("closed", repository=999), _pr_payload("opened"), _pr_payload("labeled")):
        assert _post(e, "pull_request", payload).json() == {"handled": False}

    assert _state_and_events(investigation_id) == ("open", [])


def test_a_pull_request_event_for_an_unreadable_payload_is_ignored(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    investigation_id = _add_pull_request()

    broken = {"action": "closed", "installation": {"id": INSTALLATION}, "pull_request": {"number": "x"}}

    assert _post(e, "pull_request", broken).json() == {"handled": False}
    assert _state_and_events(investigation_id)[0] == "open"
