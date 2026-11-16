"""Step 5.6: which tenants' investigations may be sent to LangSmith."""

from uuid import uuid4

import pytest
from langsmith.utils import get_env_var, tracing_is_enabled

from app.config import Settings
from app.graph.executor import GraphExecutor
from app.observability import tracing_allowed, tracing_scope
from app.store import provider_config as store
from tests.conftest import requires_postgres
from tests.jwt_helpers import TENANT_A, TENANT_B, USER_ID

OPTED_IN = uuid4()


@pytest.fixture(autouse=True)
def fresh_langsmith_env():
    """LangSmith caches the environment variables it reads: forget them so each test sees its own setting."""
    get_env_var.cache_clear()
    yield
    get_env_var.cache_clear()


def settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_tracing_off_means_nobody_is_traced():
    s = settings(langchain_tracing_v2=False, tracing_opt_in_tenants={OPTED_IN})
    assert not tracing_allowed(s, tenant_id=OPTED_IN, byok=False)
    assert not tracing_allowed(s, tenant_id=OPTED_IN, byok=True)


def test_a_tenant_on_the_platforms_keys_is_traced_when_tracing_is_on():
    s = settings(langchain_tracing_v2=True)
    assert tracing_allowed(s, tenant_id=uuid4(), byok=False)


def test_a_bring_your_own_key_tenant_is_traced_only_if_it_opted_in():
    s = settings(langchain_tracing_v2=True, tracing_opt_in_tenants={OPTED_IN})
    assert not tracing_allowed(s, tenant_id=uuid4(), byok=True)
    assert tracing_allowed(s, tenant_id=OPTED_IN, byok=True)


def test_the_opt_in_list_is_read_from_the_environment_as_json(monkeypatch):
    monkeypatch.setenv("TRACING_OPT_IN_TENANTS", f'["{OPTED_IN}"]')
    assert Settings(_env_file=None).tracing_opt_in_tenants == {OPTED_IN}
    monkeypatch.delenv("TRACING_OPT_IN_TENANTS")
    assert Settings(_env_file=None).tracing_opt_in_tenants == set()


def test_the_scope_switches_tracing_off_inside_and_restores_it_after(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    assert tracing_is_enabled()
    with tracing_scope(False):
        assert not tracing_is_enabled()
    assert tracing_is_enabled()
    with tracing_scope(True):
        assert tracing_is_enabled()  # allowed: nothing changes


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def run_executor(monkeypatch, *, tenant, byok: bool, opted_in: bool) -> bool:
    """Run ``GraphExecutor.run`` with the inner run replaced, and say whether tracing was on inside it."""
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    s = settings(langchain_tracing_v2=True, tracing_opt_in_tenants={tenant} if opted_in else set())
    seen: list[bool] = []

    async def fake_inner(self, session_factory, investigation_id, tenant_id):
        seen.append(tracing_is_enabled())

    async def fake_has_own_key(session, *, tenant_id):
        return byok

    monkeypatch.setattr(GraphExecutor, "_run", fake_inner)
    monkeypatch.setattr(store, "has_own_key", fake_has_own_key)
    executor = GraphExecutor(None, logs=None, incidents=None, history=None, models=None, settings=s)  # type: ignore[arg-type]
    await executor.run(lambda: FakeSession(), uuid4(), tenant)  # type: ignore[arg-type]
    return seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("byok", "opted_in", "expected"),
    [(False, False, True), (True, False, False), (True, True, True), (False, True, True)],
)
async def test_the_executor_applies_the_policy_to_the_whole_run(monkeypatch, byok, opted_in, expected):
    assert await run_executor(monkeypatch, tenant=TENANT_A, byok=byok, opted_in=opted_in) is expected


@pytest.mark.asyncio
async def test_tracing_is_back_on_after_a_run_that_had_it_off(monkeypatch):
    await run_executor(monkeypatch, tenant=TENANT_A, byok=True, opted_in=False)
    assert tracing_is_enabled()


# ------------------------------------------------------------------- the real query


@pytest.mark.asyncio
@requires_postgres
async def test_has_own_key_looks_at_stored_keys_only():
    from sqlalchemy import create_engine, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings
    from app.db.session import create_session_factory

    sync = create_engine(get_settings().database_url)
    with sync.begin() as conn:
        for t in (TENANT_A, TENANT_B):
            conn.execute(text("DELETE FROM ai.provider_config WHERE tenant_id = :t"), {"t": t})
        conn.execute(
            text(
                "INSERT INTO ai.provider_config (tenant_id, purpose, provider, model, updated_by) "
                "VALUES (:t, 'summary', 'groq', 'm', :u)"
            ),
            {"t": TENANT_A, "u": USER_ID},
        )  # a choice of model, but no key of its own
    engine = create_async_engine(get_settings().database_url_async)
    try:
        async with create_session_factory(engine)() as session:
            assert not await store.has_own_key(session, tenant_id=TENANT_A)
        with sync.begin() as conn:
            conn.execute(
                text(
                    "UPDATE ai.provider_config SET api_key_ciphertext = 'ciphertext', key_id = 'k1', key_last4 = 'abcd' "
                    "WHERE tenant_id = :t"
                ),
                {"t": TENANT_A},
            )
        async with create_session_factory(engine)() as session:
            assert await store.has_own_key(session, tenant_id=TENANT_A)
            assert not await store.has_own_key(session, tenant_id=TENANT_B)
    finally:
        with sync.begin() as conn:
            conn.execute(text("DELETE FROM ai.provider_config WHERE tenant_id = :t"), {"t": TENANT_A})
        sync.dispose()
        await engine.dispose()
