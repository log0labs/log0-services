"""``/api/v1/repositories`` and ``/api/v1/service-repositories`` (real app, real Postgres, fake GitHub)."""

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.security.crypto import DecryptionError, secret_context
from tests.config_api_support import TOKEN, Env, make_env
from tests.jwt_helpers import TENANT_A, TENANT_B

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

REPOS = "/api/v1/repositories"
SERVICES = "/api/v1/service-repositories"
BODY = {"owner": "acme", "name": "shop", "token": TOKEN}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    with make_env(monkeypatch) as e:
        yield e


def _connect(env: Env, *, tenant=TENANT_A, body: dict | None = None):
    return env.client.post(REPOS, json=body or BODY, headers=env.headers(tenant=tenant))


def _stored_rows() -> list[dict]:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM ai.repository_connection WHERE tenant_id = :t"), {"t": TENANT_A}).mappings().all()
    engine.dispose()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- access


def test_everything_needs_a_signed_in_user(env: Env) -> None:
    assert env.client.get(REPOS).status_code == 401
    assert env.client.post(REPOS, json=BODY).status_code == 401
    assert env.client.get(SERVICES).status_code == 401


@pytest.mark.parametrize("role", ["ENGINEER", "VIEWER"])
def test_only_an_admin_can_change_things_but_everyone_can_read(env: Env, role: str) -> None:
    headers = env.headers(role=role)
    assert env.client.post(REPOS, json=BODY, headers=headers).status_code == 403
    assert env.client.delete(f"{REPOS}/{uuid4()}", headers=headers).status_code == 403
    assert env.client.put(f"{SERVICES}/payment", json={"repositoryId": str(uuid4())}, headers=headers).status_code == 403
    assert env.client.delete(f"{SERVICES}/payment", headers=headers).status_code == 403
    assert env.client.get(REPOS, headers=headers).status_code == 200
    assert env.client.get(SERVICES, headers=headers).status_code == 200
    assert env.github.calls == []  # a refused request never reaches GitHub


# ------------------------------------------------------------------------- connecting


def test_connecting_checks_the_token_stores_it_encrypted_and_returns_no_secret(env: Env) -> None:
    response = _connect(env)

    assert response.status_code == 201
    body = response.json()
    # GitHub's spelling and default branch win over what was typed.
    assert (body["owner"], body["name"], body["defaultBranch"], body["provider"]) == ("Acme", "Shop", "trunk", "github")
    assert set(body) == {"id", "provider", "owner", "name", "defaultBranch", "createdAt"}
    assert TOKEN not in response.text and "ciphertext" not in response.text.lower()
    assert env.github.calls == [("acme", "shop", TOKEN)]  # what the client typed is what GitHub was asked with

    (row,) = _stored_rows()
    assert row["token_ciphertext"] != TOKEN and TOKEN not in row["token_ciphertext"]
    assert row["key_id"] == "k1" and row["created_by"] is not None
    assert env.box is not None
    context = secret_context(TENANT_A, "repository_connection", "Acme/Shop")
    assert env.box.decrypt(row["token_ciphertext"], key_id="k1", context=context).get_secret_value() == TOKEN


def test_the_stored_token_is_bound_to_its_tenant_and_repository(env: Env) -> None:
    _connect(env)
    (row,) = _stored_rows()
    assert env.box is not None
    for other in (secret_context(TENANT_B, "repository_connection", "Acme/Shop"), secret_context(TENANT_A, "repository_connection", "Acme/Other")):
        with pytest.raises(DecryptionError):
            env.box.decrypt(row["token_ciphertext"], key_id="k1", context=other)


@pytest.mark.parametrize(
    ("problem", "message"),
    [("bad_token", "rejected the token"), ("not_found", "not found"), ("forbidden", "not allowed")],
)
def test_github_saying_no_is_a_422_and_nothing_is_stored(env: Env, problem: str, message: str) -> None:
    env.github.error = GitHubAccessError(problem)  # type: ignore[arg-type]
    response = _connect(env)
    assert response.status_code == 422 and message in response.json()["detail"]
    assert TOKEN not in response.text
    assert _stored_rows() == []


def test_github_being_down_is_a_502_and_nothing_is_stored(env: Env) -> None:
    env.github.error = GitHubUnavailableError()
    response = _connect(env)
    assert response.status_code == 502 and TOKEN not in response.text
    assert _stored_rows() == []


def test_connecting_the_same_repository_twice_is_a_409_even_if_typed_in_another_case(env: Env) -> None:
    assert _connect(env).status_code == 201
    again = _connect(env, body={**BODY, "owner": "ACME", "name": "SHOP"})  # the fake answers "Acme/Shop" again
    assert again.status_code == 409
    assert len(_stored_rows()) == 1


def test_another_tenant_can_connect_the_same_repository(env: Env) -> None:
    assert _connect(env).status_code == 201
    assert _connect(env, tenant=TENANT_B).status_code == 201


@pytest.mark.parametrize(
    "bad",
    [
        {"owner": "../etc"},
        {"owner": "acme/evil"},
        {"owner": "-acme"},
        {"owner": ""},
        {"name": ".."},
        {"name": "shop/../x"},
        {"name": "sh op"},
        {"token": "short"},
        {"extra": "field"},
        {"baseUrl": "http://169.254.169.254"},
    ],
)
def test_malformed_requests_are_422_before_anything_is_sent_to_github(env: Env, bad: dict) -> None:
    assert _connect(env, body={**BODY, **bad}).status_code == 422
    assert env.github.calls == []


def test_without_a_master_key_connecting_is_a_503_and_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    with make_env(monkeypatch, with_master_key=False) as env:
        response = _connect(env)
        assert response.status_code == 503
        assert env.github.calls == []
        assert env.client.get(REPOS, headers=env.headers()).status_code == 200  # reading needs no key


# -------------------------------------------------------------------- listing and delete


def test_each_tenant_only_sees_and_removes_its_own_repositories(env: Env) -> None:
    mine = _connect(env).json()["id"]
    theirs = _connect(env, tenant=TENANT_B).json()["id"]

    assert [r["id"] for r in env.client.get(REPOS, headers=env.headers()).json()] == [mine]
    assert [r["id"] for r in env.client.get(REPOS, headers=env.headers(tenant=TENANT_B)).json()] == [theirs]

    assert env.client.delete(f"{REPOS}/{mine}", headers=env.headers(tenant=TENANT_B)).status_code == 404
    assert len(env.client.get(REPOS, headers=env.headers()).json()) == 1  # untouched

    assert env.client.delete(f"{REPOS}/{mine}", headers=env.headers()).status_code == 204
    assert env.client.get(REPOS, headers=env.headers()).json() == []
    assert env.client.delete(f"{REPOS}/{mine}", headers=env.headers()).status_code == 404


# ---------------------------------------------------------------------- service mappings


def test_a_service_is_mapped_listed_replaced_and_removed(env: Env) -> None:
    repo = _connect(env).json()["id"]
    headers = env.headers()

    put = env.client.put(f"{SERVICES}/payment-service", json={"repositoryId": repo, "pathPrefix": "/services/payment/"}, headers=headers)
    assert put.status_code == 200
    assert put.json() == {"serviceName": "payment-service", "repositoryId": repo, "pathPrefix": "services/payment"}

    replaced = env.client.put(f"{SERVICES}/payment-service", json={"repositoryId": repo}, headers=headers)
    assert replaced.json()["pathPrefix"] == ""
    assert len(env.client.get(SERVICES, headers=headers).json()) == 1

    assert env.client.delete(f"{SERVICES}/payment-service", headers=headers).status_code == 204
    assert env.client.get(SERVICES, headers=headers).json() == []
    assert env.client.delete(f"{SERVICES}/payment-service", headers=headers).status_code == 404


def test_a_service_cannot_be_mapped_to_another_tenants_or_a_missing_repository(env: Env) -> None:
    other_tenants = _connect(env, tenant=TENANT_B).json()["id"]
    headers = env.headers()
    for repo in (other_tenants, str(uuid4())):
        response = env.client.put(f"{SERVICES}/payment-service", json={"repositoryId": repo}, headers=headers)
        assert response.status_code == 404
    assert env.client.get(SERVICES, headers=headers).json() == []


@pytest.mark.parametrize("prefix", ["../secrets", "a/../../b", "a//b", "a\\b", "./a", "a/./b", "x" * 501, "a\x00b"])
def test_a_path_prefix_that_could_leave_the_checkout_is_refused(env: Env, prefix: str) -> None:
    repo = _connect(env).json()["id"]
    response = env.client.put(f"{SERVICES}/payment-service", json={"repositoryId": repo, "pathPrefix": prefix}, headers=env.headers())
    assert response.status_code == 422
    assert env.client.get(SERVICES, headers=env.headers()).json() == []


def test_mappings_are_per_tenant_and_disconnecting_a_repository_removes_its_mappings(env: Env) -> None:
    mine = _connect(env).json()["id"]
    theirs = _connect(env, tenant=TENANT_B).json()["id"]
    env.client.put(f"{SERVICES}/payment-service", json={"repositoryId": mine}, headers=env.headers())
    env.client.put(f"{SERVICES}/payment-service", json={"repositoryId": theirs}, headers=env.headers(tenant=TENANT_B))  # same name, other tenant

    assert [m["repositoryId"] for m in env.client.get(SERVICES, headers=env.headers()).json()] == [mine]
    assert env.client.delete(f"{SERVICES}/payment-service", headers=env.headers(tenant=TENANT_B)).status_code == 204
    assert len(env.client.get(SERVICES, headers=env.headers()).json()) == 1  # mine still there

    env.client.delete(f"{REPOS}/{mine}", headers=env.headers())
    assert env.client.get(SERVICES, headers=env.headers()).json() == []
