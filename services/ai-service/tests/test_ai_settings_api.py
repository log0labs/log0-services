"""``/api/v1/ai-settings`` (real app, real Postgres, fake model provider)."""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from app.config import Settings, get_settings
from app.llm.connection_check import ConnectionCheck
from app.security.crypto import secret_context
from tests.config_api_support import API_KEY, Env, make_env
from tests.jwt_helpers import TENANT_A, TENANT_B

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)

URL = "/api/v1/ai-settings"
PURPOSES = ["summary", "triage", "investigation", "evaluation"]
BODY = {"provider": "groq", "model": "openai/gpt-oss-120b", "apiKey": API_KEY, "temperature": 0.2, "maxTokens": 2000}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    with make_env(monkeypatch) as e:
        yield e


def _put(env: Env, body: dict, *, purpose: str = "investigation", tenant=TENANT_A, role: str = "ADMIN"):
    return env.client.put(f"{URL}/{purpose}", json=body, headers=env.headers(tenant=tenant, role=role))


def _row(purpose: str = "investigation", tenant=TENANT_A) -> dict | None:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM ai.provider_config WHERE tenant_id = :t AND purpose = :p"), {"t": tenant, "p": purpose}
        ).mappings().first()
    engine.dispose()
    return dict(row) if row else None


# ------------------------------------------------------------------------------ reading


def test_reading_needs_a_user_and_with_nothing_saved_every_purpose_shows_the_platform_default(env: Env) -> None:
    assert env.client.get(URL).status_code == 401

    response = env.client.get(URL, headers=env.headers(role="VIEWER"))  # a viewer may read
    assert response.status_code == 200
    entries = response.json()
    assert [e["purpose"] for e in entries] == PURPOSES
    assert all(e["source"] == "platform" and e["keyLast4"] is None for e in entries)
    platform = get_settings().llm_models
    assert [(e["provider"], e["model"]) for e in entries] == [(platform[p].provider, platform[p].model) for p in platform]


# ------------------------------------------------------------------------------ saving


def test_saving_stores_the_key_encrypted_and_never_returns_it(env: Env) -> None:
    response = _put(env, BODY)

    assert response.status_code == 200
    body = response.json()
    assert (body["source"], body["provider"], body["model"], body["keyLast4"]) == ("tenant", "groq", "openai/gpt-oss-120b", "ABCD")
    assert (body["temperature"], body["maxTokens"]) == (0.2, 2000)
    assert API_KEY not in response.text and "apiKey" not in response.text and "ciphertext" not in response.text.lower()

    row = _row()
    assert row is not None and API_KEY not in row["api_key_ciphertext"] and row["key_id"] == "k1"
    assert env.box is not None
    context = secret_context(TENANT_A, "provider_config", "investigation")
    assert env.box.decrypt(row["api_key_ciphertext"], key_id="k1", context=context).get_secret_value() == API_KEY
    assert row["updated_by"] is not None


def test_the_saved_setting_shows_in_the_list_and_only_for_its_own_tenant(env: Env) -> None:
    _put(env, BODY)

    mine = {e["purpose"]: e for e in env.client.get(URL, headers=env.headers()).json()}
    assert mine["investigation"]["source"] == "tenant" and mine["investigation"]["keyLast4"] == "ABCD"
    assert all(mine[p]["source"] == "platform" for p in ("summary", "triage", "evaluation"))

    theirs = {e["purpose"]: e for e in env.client.get(URL, headers=env.headers(tenant=TENANT_B)).json()}
    assert all(e["source"] == "platform" and e["keyLast4"] is None for e in theirs.values())


@pytest.mark.parametrize("role", ["ENGINEER", "VIEWER"])
def test_only_an_admin_can_save_delete_or_test(env: Env, role: str) -> None:
    assert _put(env, BODY, role=role).status_code == 403
    assert env.client.delete(f"{URL}/investigation", headers=env.headers(role=role)).status_code == 403
    assert env.client.post(f"{URL}/investigation/test", json=BODY, headers=env.headers(role=role)).status_code == 403
    assert env.checker.calls == [] and _row() is None


@pytest.mark.parametrize(
    "bad",
    [
        {"provider": "skynet"},
        {"provider": ""},
        {"model": ""},
        {"apiKey": "short"},
        {"temperature": 3},
        {"temperature": -1},
        {"maxTokens": 0},
        {"maxTokens": 10**7},
        {"baseUrl": "http://169.254.169.254"},  # not supported: a tenant-chosen URL would make the server call anywhere
        {"unknown": 1},
    ],
)
def test_invalid_settings_are_422_and_nothing_is_saved(env: Env, bad: dict) -> None:
    assert _put(env, {**BODY, **bad}).status_code == 422
    assert _row() is None


def test_an_unknown_purpose_is_422(env: Env) -> None:
    assert _put(env, BODY, purpose="chat").status_code == 422


def test_a_key_is_not_needed_to_choose_a_model(env: Env) -> None:
    response = _put(env, {"provider": "groq", "model": "openai/gpt-oss-20b"})  # use the platform's key
    assert response.status_code == 200 and response.json()["keyLast4"] is None
    row = _row()
    assert row is not None and row["api_key_ciphertext"] is None and row["key_id"] is None


def test_saving_again_without_a_key_keeps_the_stored_one(env: Env) -> None:
    _put(env, BODY)
    before = _row()
    response = _put(env, {"provider": "groq", "model": "openai/gpt-oss-20b"})  # change only the model
    after = _row()

    assert response.json()["keyLast4"] == "ABCD" and response.json()["model"] == "openai/gpt-oss-20b"
    assert after is not None and before is not None
    assert after["api_key_ciphertext"] == before["api_key_ciphertext"]  # not re-encrypted, not lost


def test_a_new_key_replaces_the_old_one(env: Env) -> None:
    _put(env, BODY)
    new_key = "gsk_live_zzzzzzzzzzzzzzzzWXYZ"
    response = _put(env, {**BODY, "apiKey": new_key})
    assert response.json()["keyLast4"] == "WXYZ"
    row = _row()
    assert row is not None and env.box is not None
    context = secret_context(TENANT_A, "provider_config", "investigation")
    assert env.box.decrypt(row["api_key_ciphertext"], key_id="k1", context=context).get_secret_value() == new_key


def test_changing_the_provider_without_a_key_drops_the_old_providers_key(env: Env) -> None:
    _put(env, BODY)
    response = _put(env, {"provider": "openai", "model": "gpt-5-mini"})
    assert response.status_code == 200 and response.json()["keyLast4"] is None
    row = _row()
    assert row is not None and row["api_key_ciphertext"] is None


def test_without_a_master_key_a_key_cannot_be_saved_but_a_model_choice_can(monkeypatch: pytest.MonkeyPatch) -> None:
    with make_env(monkeypatch, with_master_key=False) as env:
        assert _put(env, BODY).status_code == 503
        assert _row() is None
        assert _put(env, {"provider": "groq", "model": "openai/gpt-oss-20b"}).status_code == 200


# ----------------------------------------------------------------------------- deleting


def test_deleting_returns_the_purpose_to_the_platform_default_and_removes_the_key(env: Env) -> None:
    _put(env, BODY)
    assert env.client.delete(f"{URL}/investigation", headers=env.headers(tenant=TENANT_B)).status_code == 404  # not theirs
    assert _row() is not None

    assert env.client.delete(f"{URL}/investigation", headers=env.headers()).status_code == 204
    assert _row() is None
    assert env.client.delete(f"{URL}/investigation", headers=env.headers()).status_code == 404
    entries = {e["purpose"]: e for e in env.client.get(URL, headers=env.headers()).json()}
    assert entries["investigation"]["source"] == "platform"


# ------------------------------------------------------------------------------ testing


def test_a_connection_test_uses_the_key_in_the_request_and_saves_nothing(env: Env) -> None:
    response = env.client.post(f"{URL}/investigation/test", json=BODY, headers=env.headers())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "provider": "groq", "model": "openai/gpt-oss-120b", "latencyMs": 42, "error": None}
    assert env.checker.calls == [("groq", "openai/gpt-oss-120b", API_KEY)]
    assert API_KEY not in response.text
    assert _row() is None


def test_a_failed_test_reports_only_the_category(env: Env) -> None:
    env.checker.result = ConnectionCheck(ok=False, latency_ms=300, error="auth")
    response = env.client.post(f"{URL}/investigation/test", json=BODY, headers=env.headers())
    assert response.status_code == 200
    assert (response.json()["ok"], response.json()["error"]) == (False, "auth")
    assert API_KEY not in response.text


def test_without_a_key_in_the_request_the_saved_key_is_used_if_the_provider_matches(env: Env) -> None:
    _put(env, BODY)
    response = env.client.post(
        f"{URL}/investigation/test", json={"provider": "groq", "model": "openai/gpt-oss-20b"}, headers=env.headers()
    )
    assert response.status_code == 200
    assert env.checker.calls == [("groq", "openai/gpt-oss-20b", API_KEY)]  # decrypted for the call, in memory only


def test_another_tenants_saved_key_is_never_used(env: Env) -> None:
    _put(env, BODY, tenant=TENANT_B)  # tenant B saved a key; tenant A has none
    env.client.post(f"{URL}/investigation/test", json={"provider": "groq", "model": "m"}, headers=env.headers())
    assert env.checker.calls == [("groq", "m", get_settings().groq_api_key.get_secret_value())]  # the platform key, not B's


def test_without_any_key_the_platform_key_is_used_and_if_there_is_none_it_is_a_422(env: Env) -> None:
    from app.config import get_settings as dependency

    env.app.dependency_overrides[dependency] = lambda: Settings(_env_file=None, openai_api_key=None, groq_api_key="platform-groq-key")  # type: ignore[call-arg, union-attr]
    ok = env.client.post(f"{URL}/triage/test", json={"provider": "groq", "model": "m"}, headers=env.headers())
    assert ok.status_code == 200 and env.checker.calls == [("groq", "m", "platform-groq-key")]

    env.checker.calls.clear()
    missing = env.client.post(f"{URL}/triage/test", json={"provider": "openai", "model": "m"}, headers=env.headers())
    assert missing.status_code == 422 and "apiKey" in missing.json()["detail"]
    assert env.checker.calls == []


def test_a_saved_key_that_can_no_longer_be_read_is_a_409_not_a_crash(env: Env) -> None:
    _put(env, BODY)
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:  # simulate a retired master key
        conn.execute(text("UPDATE ai.provider_config SET key_id = 'k0' WHERE tenant_id = :t"), {"t": TENANT_A})
    engine.dispose()

    response = env.client.post(f"{URL}/investigation/test", json={"provider": "groq", "model": "m"}, headers=env.headers())
    assert response.status_code == 409 and "enter it again" in response.json()["detail"]
    assert env.checker.calls == []
