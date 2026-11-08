"""PurposeChatModels: builds a chat model per purpose once, with the right generation settings."""

from typing import Any

import pytest

from app.config import get_settings
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
