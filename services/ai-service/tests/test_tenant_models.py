"""A workspace's own model choices drive its investigations; everyone else keeps the platform models."""

from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings, get_settings
from app.db.session import create_session_factory
from app.graph.executor import GraphExecutor
from app.llm.model_spec import ModelSpec
from app.llm.models import PurposeChatModels
from app.llm.purpose import Purpose
from app.llm.resolver import PlatformLlmResolver, TenantLlmResolver
from app.runner import SessionFactory
from app.security.crypto import SecretBox, secret_context
from app.store import provider_config as store
from tests.jwt_helpers import TENANT_A

KEY = Fernet.generate_key().decode()


def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        jwt_secret="x" * 40,
        internal_service_token="t" * 20,
        groq_api_key="platform-groq",
        google_api_key="platform-google",
        llm_investigation="groq:openai/gpt-oss-120b",
        llm_triage="groq:openai/gpt-oss-20b",
        llm_investigation_output_method="json_schema",
    )


# ------------------------------------------------------------------------------ resolver


def test_a_purpose_without_a_workspace_choice_uses_the_platform_model_and_key():
    platform = PlatformLlmResolver(settings())
    resolver = TenantLlmResolver(platform, {}, {})
    assert resolver.model_for(Purpose.TRIAGE) == ModelSpec(provider="groq", model="openai/gpt-oss-20b")
    assert resolver.api_key_for("groq").get_secret_value() == "platform-groq"
    assert resolver.is_platform_model(Purpose.TRIAGE)


def test_the_workspace_choice_and_key_win_over_the_platform():
    chosen = ModelSpec(provider="google_genai", model="gemini-3.5-flash-lite")
    resolver = TenantLlmResolver(PlatformLlmResolver(settings()), {Purpose.INVESTIGATION: chosen}, {"google_genai": SecretStr("tenant-google")})
    assert resolver.model_for(Purpose.INVESTIGATION) == chosen
    assert resolver.api_key_for("google_genai").get_secret_value() == "tenant-google"
    assert resolver.api_key_for("groq").get_secret_value() == "platform-groq"  # no key saved for groq: the platform's
    assert not resolver.is_platform_model(Purpose.INVESTIGATION)


def test_a_workspace_model_gets_the_portable_output_method_and_the_platform_model_keeps_its_own():
    cfg = settings()
    chosen = ModelSpec(provider="google_genai", model="gemini-3.5-flash-lite")
    tenant = PurposeChatModels(cfg, TenantLlmResolver(PlatformLlmResolver(cfg), {Purpose.INVESTIGATION: chosen}, {}))
    assert tenant.structured_method(Purpose.INVESTIGATION) == "function_calling"
    assert PurposeChatModels(cfg).structured_method(Purpose.INVESTIGATION) == "json_schema"  # the platform's own setting


def test_a_workspace_temperature_and_token_limit_reach_the_model():
    cfg = settings()
    resolver = TenantLlmResolver(PlatformLlmResolver(cfg), {}, {})
    models = PurposeChatModels(cfg, resolver, overrides={Purpose.TRIAGE: (0.7, 777)})
    model = models.for_purpose(Purpose.TRIAGE)
    assert getattr(model, "temperature", None) == 0.7
    assert 777 in (getattr(model, "max_tokens", None), getattr(model, "max_output_tokens", None))


# ----------------------------------------------------------------------------- executor


@pytest_asyncio.fixture
async def factory() -> AsyncGenerator[SessionFactory]:
    engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
    f = create_session_factory(engine)
    try:
        yield f
    finally:
        async with f() as session:
            await session.execute(text("DELETE FROM ai.provider_config WHERE tenant_id = :t"), {"t": TENANT_A})
            await session.commit()
        await engine.dispose()


def _executor(box: SecretBox | None, cfg: Settings) -> GraphExecutor:
    platform = PurposeChatModels(cfg)
    return GraphExecutor(
        None,  # type: ignore[arg-type]  # the graph is not used by _models_for
        logs=None,  # type: ignore[arg-type]
        incidents=None,  # type: ignore[arg-type]
        history=None,  # type: ignore[arg-type]
        models=platform,
        settings=cfg,
        secret_box=box,
    )


@pytest.mark.asyncio
async def test_a_workspace_without_settings_gets_the_shared_platform_models(factory):
    ex = _executor(None, settings())
    assert await ex._models_for(factory, uuid4()) is ex._models


@pytest.mark.asyncio
async def test_a_workspace_with_settings_gets_its_own_models_with_its_decrypted_key(factory):
    box = SecretBox({"k1": KEY}, "k1")
    cfg = settings()
    encrypted = box.encrypt("tenant-google-key", context=secret_context(TENANT_A, "provider_config", "investigation"))
    async with factory() as session:
        await store.upsert(
            session, tenant_id=TENANT_A, purpose="investigation", provider="google_genai", model="gemini-3.5-flash-lite",
            key=encrypted, temperature=0.2, max_tokens=4000, updated_by=UUID(int=1),
        )
    ex = _executor(box, cfg)

    models = await ex._models_for(factory, TENANT_A)

    assert models is not ex._models
    assert models.structured_method(Purpose.INVESTIGATION) == "function_calling"
    resolver = models._resolver  # type: ignore[attr-defined]
    assert resolver.model_for(Purpose.INVESTIGATION) == ModelSpec(provider="google_genai", model="gemini-3.5-flash-lite")
    assert resolver.api_key_for("google_genai").get_secret_value() == "tenant-google-key"
    assert resolver.model_for(Purpose.TRIAGE) == ModelSpec(provider="groq", model="openai/gpt-oss-20b")  # not chosen: platform


@pytest.mark.asyncio
async def test_a_key_that_cannot_be_read_falls_back_to_the_platform_key(factory):
    box = SecretBox({"k1": KEY}, "k1")
    wrong_context = box.encrypt("tenant-key", context=secret_context(uuid4(), "provider_config", "investigation"))  # another tenant's
    async with factory() as session:
        await store.upsert(
            session, tenant_id=TENANT_A, purpose="investigation", provider="google_genai", model="gemini-3.5-flash-lite",
            key=wrong_context, temperature=None, max_tokens=None, updated_by=UUID(int=1),
        )
    models = await _executor(box, settings())._models_for(factory, TENANT_A)
    assert models._resolver.api_key_for("google_genai").get_secret_value() == "platform-google"  # type: ignore[attr-defined]
