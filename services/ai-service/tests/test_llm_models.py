"""PurposeChatModels: builds a chat model per purpose once, with the right generation settings."""

from typing import Any

import pytest

from app.config import Settings, get_settings
from app.llm import models as models_module
from app.llm.generation import generation_for_purpose
from app.llm.models import PurposeChatModels
from app.llm.purpose import Purpose


def test_triage_is_deterministic_other_purposes_use_the_configured_temperature() -> None:
    settings = get_settings()
    assert generation_for_purpose(settings, Purpose.TRIAGE).temperature == 0.0
    for purpose in (Purpose.SUMMARY, Purpose.INVESTIGATION, Purpose.EVALUATION):
        assert generation_for_purpose(settings, purpose).temperature == settings.llm_temperature


def test_each_purpose_is_built_once_and_then_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[Purpose, Any]] = []

    def fake_factory(resolver: Any, purpose: Purpose, generation: Any) -> object:
        built.append((purpose, generation))
        return object()

    monkeypatch.setattr(models_module, "chat_model_for_purpose", fake_factory)
    models = PurposeChatModels(get_settings())

    first = models.for_purpose(Purpose.TRIAGE)
    assert models.for_purpose(Purpose.TRIAGE) is first  # cached
    other = models.for_purpose(Purpose.INVESTIGATION)
    assert other is not first  # a different purpose gets its own model
    assert [p for p, _ in built] == [Purpose.TRIAGE, Purpose.INVESTIGATION]
    assert built[0][1].temperature == 0.0  # the generation settings reached the factory


def test_building_is_lazy_so_an_unused_purpose_never_needs_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Purpose] = []
    monkeypatch.setattr(
        models_module, "chat_model_for_purpose", lambda r, p, g: calls.append(p) or object()
    )
    PurposeChatModels(get_settings())  # constructing builds nothing
    assert calls == []


def test_structured_output_methods_default_to_what_the_configured_groq_models_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a developer's .env may override these; the test is about the code defaults
    for name in ("LLM_TRIAGE", "LLM_INVESTIGATION", "LLM_TRIAGE_OUTPUT_METHOD", "LLM_INVESTIGATION_OUTPUT_METHOD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    models = PurposeChatModels(get_settings())
    assert models.structured_method(Purpose.TRIAGE) == "json_schema"  # gpt-oss
    assert models.structured_method(Purpose.INVESTIGATION) == "function_calling"  # qwen rejects json_schema
    assert models.structured_method(Purpose.SUMMARY) == "function_calling"  # LangChain's default


def test_the_method_can_be_changed_with_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_INVESTIGATION", "groq:openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_INVESTIGATION_OUTPUT_METHOD", "json_schema")
    get_settings.cache_clear()
    assert PurposeChatModels(get_settings()).structured_method(Purpose.INVESTIGATION) == "json_schema"



def test_the_strong_model_is_used_only_when_configured_and_the_workspace_has_not_chosen_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.model_spec import ModelSpec

    built: list[str] = []
    monkeypatch.setattr(models_module, "chat_model_for_purpose", lambda r, p, g: object())
    monkeypatch.setattr(models_module, "build_chat_model", lambda **kw: built.append(kw["model"]) or object())

    plain = get_settings().model_copy(update={"llm_root_cause": None})
    models = PurposeChatModels(plain)
    assert models.for_purpose(Purpose.INVESTIGATION, strong=True) is models.for_purpose(Purpose.INVESTIGATION)  # nothing configured

    configured = get_settings().model_copy(update={"llm_root_cause": ModelSpec(provider="google_genai", model="strong-1")})
    models = PurposeChatModels(configured, resolver=_FixedResolver(configured, "google_genai"))
    first = models.for_purpose(Purpose.INVESTIGATION, strong=True)
    assert models.for_purpose(Purpose.INVESTIGATION, strong=True) is first and built == ["strong-1"]  # built once
    assert models.for_purpose(Purpose.TRIAGE, strong=True) is not first  # only the investigation purpose has a strong model

    own = _FixedResolver(configured, "google_genai", chosen=ModelSpec(provider="groq", model="theirs"))
    assert PurposeChatModels(configured, resolver=own).for_purpose(Purpose.INVESTIGATION, strong=True) is not first and built == ["strong-1"]


class _FixedResolver:
    def __init__(self, settings: Settings, provider: str, chosen: Any = None) -> None:
        self._settings, self._chosen = settings, chosen

    def model_for(self, purpose: Purpose) -> Any:
        return self._chosen if self._chosen is not None and purpose == Purpose.INVESTIGATION else self._settings.llm_models[purpose]

    def api_key_for(self, provider: str) -> Any:
        from pydantic import SecretStr

        return SecretStr("k")
