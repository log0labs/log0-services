"""``/api/v1/github`` (real app, real Postgres, fake GitHub App client): linking, listing, per-repository writes."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from app.api.dependencies import get_github_app
from app.config import get_settings
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import GitHubAppNotConfiguredError
from app.integrations.github_app_client import InstallationInfo, InstalledRepository
from app.security.install_state import issue_state
from tests.config_api_support import TOKEN, Env, make_env
from tests.jwt_helpers import TENANT_A, TENANT_B, USER_ID

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

BASE = "/api/v1/github"
REPOS = "/api/v1/repositories"
INSTALLATION = 4242


class FakeAuth:
    def __init__(self) -> None:
        self.configured = True
        self.forgotten: list[int] = []

    def forget(self, installation_id: int) -> None:
        self.forgotten.append(installation_id)


class FakeAppClient:
    def __init__(self) -> None:
        self.auth = FakeAuth()
        self.info = InstallationInfo(INSTALLATION, "acme", "Organization", False)
        self.repositories = [InstalledRepository(7, "Acme", "Shop", "trunk", True)]
        self.error: Exception | None = None
        self.listed: list[int] = []

    async def get_installation(self, installation_id: int) -> InstallationInfo:
        if self.error:
            raise self.error
        return InstallationInfo(installation_id, self.info.account_login, self.info.account_type, self.info.suspended)

    async def list_repositories(self, installation_id: int) -> list[InstalledRepository]:
        self.listed.append(installation_id)
        if self.error:
            raise self.error
        return self.repositories


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Env, FakeAppClient]]:
    monkeypatch.setenv("GITHUB_APP_SLUG", "log0-test")
    with make_env(monkeypatch) as e:
        fake = FakeAppClient()
        e.app.dependency_overrides[get_github_app] = lambda: fake  # type: ignore[attr-defined]
        yield e, fake


def _state(*, tenant: UUID = TENANT_A, user: UUID = USER_ID, **kwargs) -> str:
    return issue_state(get_settings().jwt_secret.get_secret_value(), tenant_id=tenant, user_id=user, **kwargs)


def _link(env: Env, *, tenant: UUID = TENANT_A, state: str | None = None, installation_id: int = INSTALLATION):
    body = {"installationId": installation_id, "state": state or _state(tenant=tenant)}
    return env.client.post(f"{BASE}/installations", json=body, headers=env.headers(tenant=tenant))


def _connect_repository(env: Env, *, tenant: UUID = TENANT_A) -> str:
    response = env.client.post(REPOS, json={"owner": "acme", "name": "shop", "token": TOKEN}, headers=env.headers(tenant=tenant))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _installations_in_db() -> list[tuple[UUID, int]]:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT tenant_id, installation_id FROM ai.github_installation WHERE installation_id = :i"), {"i": INSTALLATION}).all()
    engine.dispose()
    return [(r[0], r[1]) for r in rows]


# --------------------------------------------------------------------------- access


def test_everything_needs_an_admin(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    assert e.client.get(f"{BASE}/app").status_code == 401
    for role in ("ENGINEER", "VIEWER"):
        headers = e.headers(role=role)
        assert e.client.get(f"{BASE}/app", headers=headers).status_code == 403
        assert e.client.get(f"{BASE}/installations", headers=headers).status_code == 403
        assert e.client.post(f"{BASE}/installations", json={"installationId": 1, "state": "x"}, headers=headers).status_code == 403
        assert e.client.put(f"{BASE}/repositories/{uuid4()}/writes", json={"enabled": True}, headers=headers).status_code == 403


# ------------------------------------------------------------------------- app status


def test_status_gives_an_install_link_that_carries_a_state_for_this_admin(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env

    body = e.client.get(f"{BASE}/app", headers=e.headers()).json()

    assert body["configured"] is True and body["writesEnabled"] is False
    assert body["installUrl"].startswith("https://github.com/apps/log0-test/installations/new?state=")


def test_status_without_the_app_configured_has_no_link(env: tuple[Env, FakeAppClient]) -> None:
    e, fake = env
    fake.auth.configured = False

    body = e.client.get(f"{BASE}/app", headers=e.headers()).json()

    assert body["configured"] is False and body["installUrl"] is None


# ------------------------------------------------------------------------- linking


def test_linking_an_installation_stores_it_for_the_tenant(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env

    response = _link(e)

    assert response.status_code == 201
    assert response.json()["accountLogin"] == "acme" and response.json()["suspended"] is False
    assert _installations_in_db() == [(TENANT_A, INSTALLATION)]
    assert [i["installationId"] for i in e.client.get(f"{BASE}/installations", headers=e.headers()).json()] == [INSTALLATION]


def test_linking_twice_from_the_same_tenant_is_fine(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    assert _link(e).status_code == 201
    assert _link(e).status_code == 201
    assert len(_installations_in_db()) == 1


def test_another_tenant_cannot_claim_a_linked_installation(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    assert _link(e).status_code == 201

    response = _link(e, tenant=TENANT_B)

    assert response.status_code == 404
    assert _installations_in_db() == [(TENANT_A, INSTALLATION)]
    assert e.client.get(f"{BASE}/installations", headers=e.headers(tenant=TENANT_B)).json() == []


@pytest.mark.parametrize("make_state", [lambda: "garbage", lambda: _state(tenant=TENANT_B), lambda: _state(user=uuid4()), lambda: _state(now=datetime(2020, 1, 1, tzinfo=UTC))])
def test_a_state_that_is_not_ours_or_is_old_is_refused_before_asking_github(env: tuple[Env, FakeAppClient], make_state) -> None:
    e, fake = env
    fake.error = AssertionError("GitHub must not be asked")

    response = _link(e, state=make_state())

    assert response.status_code == 422
    assert _installations_in_db() == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (GitHubAccessError("not_found"), 404),
        (GitHubAccessError("forbidden"), 422),
        (GitHubUnavailableError(), 502),
        (GitHubAppNotConfiguredError("x"), 503),
    ],
)
def test_github_problems_map_to_clear_statuses(env: tuple[Env, FakeAppClient], error: Exception, status: int) -> None:
    e, fake = env
    fake.error = error

    assert _link(e).status_code == status
    assert _installations_in_db() == []


def test_unlinking_removes_it_and_forgets_cached_tokens(env: tuple[Env, FakeAppClient]) -> None:
    e, fake = env
    _link(e)

    assert e.client.delete(f"{BASE}/installations/{INSTALLATION}", headers=e.headers()).status_code == 204

    assert _installations_in_db() == []
    assert fake.auth.forgotten == [INSTALLATION]
    assert e.client.delete(f"{BASE}/installations/{INSTALLATION}", headers=e.headers()).status_code == 404


def test_another_tenant_cannot_unlink_or_browse_an_installation(env: tuple[Env, FakeAppClient]) -> None:
    e, fake = env
    _link(e)
    other = e.headers(tenant=TENANT_B)

    assert e.client.delete(f"{BASE}/installations/{INSTALLATION}", headers=other).status_code == 404
    assert e.client.get(f"{BASE}/installations/{INSTALLATION}/repositories", headers=other).status_code == 404
    assert fake.listed == []
    assert len(_installations_in_db()) == 1


# ---------------------------------------------------------------- repositories and writes


def test_repositories_show_which_are_connected_and_writable(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    _link(e)

    before = e.client.get(f"{BASE}/installations/{INSTALLATION}/repositories", headers=e.headers()).json()
    repository_id = _connect_repository(e)
    e.client.put(f"{BASE}/repositories/{repository_id}/writes", json={"enabled": True}, headers=e.headers())
    after = e.client.get(f"{BASE}/installations/{INSTALLATION}/repositories", headers=e.headers()).json()

    assert before == [{"githubId": 7, "owner": "Acme", "name": "Shop", "defaultBranch": "trunk", "private": True, "repositoryId": None, "writesEnabled": False}]
    assert after[0]["repositoryId"] == repository_id and after[0]["writesEnabled"] is True


def test_enabling_writes_links_the_installation_and_disabling_turns_them_off(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    _link(e)
    repository_id = _connect_repository(e)
    url = f"{BASE}/repositories/{repository_id}/writes"

    on = e.client.put(url, json={"enabled": True}, headers=e.headers()).json()
    off = e.client.put(url, json={"enabled": False}, headers=e.headers()).json()

    assert on["prWritesEnabled"] is True and on["installationId"] == INSTALLATION
    assert off["prWritesEnabled"] is False
    listed = e.client.get(REPOS, headers=e.headers()).json()
    assert listed[0]["prWritesEnabled"] is False


def test_writes_stay_off_for_a_repository_the_installation_cannot_reach(env: tuple[Env, FakeAppClient]) -> None:
    e, fake = env
    _link(e)
    repository_id = _connect_repository(e)
    fake.repositories = [InstalledRepository(9, "acme", "another", "main", False)]

    response = e.client.put(f"{BASE}/repositories/{repository_id}/writes", json={"enabled": True}, headers=e.headers())

    assert response.status_code == 422
    assert e.client.get(REPOS, headers=e.headers()).json()[0]["prWritesEnabled"] is False


def test_writes_cannot_be_enabled_without_an_installation(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    repository_id = _connect_repository(e)

    assert e.client.put(f"{BASE}/repositories/{repository_id}/writes", json={"enabled": True}, headers=e.headers()).status_code == 422


def test_a_suspended_installation_gives_no_write_access(env: tuple[Env, FakeAppClient]) -> None:
    e, fake = env
    fake.info = InstallationInfo(INSTALLATION, "acme", "Organization", True)
    _link(e)
    repository_id = _connect_repository(e)

    assert e.client.put(f"{BASE}/repositories/{repository_id}/writes", json={"enabled": True}, headers=e.headers()).status_code == 422
    assert fake.listed == []


def test_another_tenants_repository_is_not_found_for_writes(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    _link(e)
    repository_id = _connect_repository(e)

    assert e.client.put(f"{BASE}/repositories/{repository_id}/writes", json={"enabled": True}, headers=e.headers(tenant=TENANT_B)).status_code == 404


def test_an_attacker_who_claims_a_foreign_installation_still_cannot_enable_writes_for_a_repository_they_cannot_connect(
    env: tuple[Env, FakeAppClient],
) -> None:
    e, fake = env
    fake.repositories = [InstalledRepository(7, "victim", "private-app", "main", True)]
    _link(e, tenant=TENANT_B)
    own = e.client.post(REPOS, json={"owner": "acme", "name": "shop", "token": TOKEN}, headers=e.headers(tenant=TENANT_B)).json()["id"]

    response = e.client.put(f"{BASE}/repositories/{own}/writes", json={"enabled": True}, headers=e.headers(tenant=TENANT_B))

    assert response.status_code == 422  # the repository they connected is not one the installation reaches


def test_state_tokens_are_not_access_tokens(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    response = e.client.get(REPOS, headers={"Authorization": f"Bearer {_state()}"})

    assert response.status_code == 401


def test_the_audit_log_shows_an_admin_only_their_own_workspace_writes(env: tuple[Env, FakeAppClient]) -> None:
    e, _ = env
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ai.write_audit WHERE tenant_id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B})
        for tenant, action in ((TENANT_A, "branch.create"), (TENANT_A, "pull_request.open"), (TENANT_B, "branch.create")):
            conn.execute(
                text("INSERT INTO ai.write_audit (tenant_id, action, repository, result) VALUES (:t, :a, 'acme/shop', 'ok')"),
                {"t": tenant, "a": action},
            )
    try:
        mine = e.client.get(f"{BASE}/audit", headers=e.headers()).json()
        assert [row["action"] for row in mine] == ["pull_request.open", "branch.create"]  # newest first, only tenant A
        assert e.client.get(f"{BASE}/audit", headers=e.headers(role="ENGINEER")).status_code == 403
        assert e.client.get(f"{BASE}/audit").status_code == 401
        assert e.client.get(f"{BASE}/audit?limit=0", headers=e.headers()).status_code == 422
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ai.write_audit WHERE tenant_id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B})
        engine.dispose()
