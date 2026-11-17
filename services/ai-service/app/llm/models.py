"""One object that hands out a ready chat model per purpose.

Graph nodes should not know about providers, API keys or settings. They ask
``runtime.context.models.for_purpose(Purpose.TRIAGE)`` and get a LangChain chat model.
Tests pass a fake with the same method, so no node test ever touches the network.
"""

from langchain_core.language_models import BaseChatModel

from app.config import Settings
from app.llm.factory import GenerationConfig, build_chat_model, chat_model_for_purpose
from app.llm.generation import generation_for_purpose
from app.llm.purpose import Purpose, StructuredOutputMethod
from app.llm.resolver import LlmResolver, PlatformLlmResolver


class PurposeChatModels:
    """Builds the chat model for a purpose on first use and reuses it afterwards.

    A chat model holds an HTTP client and is safe to share between investigations, so
    building one per call would only waste time. The cache is filled lazily, which also
    means a missing API key for a purpose nobody uses never raises.
    """

    def __init__(
        self,
        settings: Settings,
        resolver: LlmResolver | None = None,
        *,
        overrides: dict[Purpose, tuple[float | None, int | None]] | None = None,
    ) -> None:
        """``overrides`` is a workspace's own (temperature, max tokens) per purpose; None keeps the platform value."""
        self._settings = settings
        self._overrides = overrides or {}
        self._resolver = resolver or PlatformLlmResolver(settings)
        self._cache: dict[Purpose, BaseChatModel] = {}
        self._strong: BaseChatModel | None = None
    
    def for_purpose(self, purpose: Purpose, *, strong: bool = False) -> BaseChatModel:
        """``strong`` asks for the root-cause model when one is configured and the workspace has not chosen its own."""
        platform_choice = self._resolver.model_for(purpose) == self._settings.llm_models[purpose]
        if strong and purpose == Purpose.INVESTIGATION and self._settings.llm_root_cause is not None and platform_choice:
            return self._strong_model()
        model = self._cache.get(purpose)
        if model is None:
            generation = generation_for_purpose(self._settings, purpose)
            temperature, max_tokens = self._overrides.get(purpose, (None, None))
            if temperature is not None or max_tokens is not None:
                generation = GenerationConfig(
                    temperature=generation.temperature if temperature is None else temperature,
                    max_tokens=generation.max_tokens if max_tokens is None else max_tokens,
                    timeout_seconds=generation.timeout_seconds,
                )
            model = chat_model_for_purpose(self._resolver, purpose, generation)
            self._cache[purpose] = model
        return model
    
    def _strong_model(self) -> BaseChatModel:
        if self._strong is None:
            spec = self._settings.llm_root_cause
            assert spec is not None
            self._strong = build_chat_model(
                provider=spec.provider,
                model=spec.model,
                api_key=self._resolver.api_key_for(spec.provider).get_secret_value(),
                generation=generation_for_purpose(self._settings, Purpose.INVESTIGATION),
            )
        return self._strong

    def structured_method(self, purpose: Purpose) -> StructuredOutputMethod:
        """How to ask this purpose's model for structured output (it depends on the model)."""
        chosen = self._resolver.model_for(purpose)
        if chosen != self._settings.llm_models[purpose]:
            # the platform setting describes the platform model; a workspace's own model gets the portable method
            return "function_calling"
        return self._settings.structured_output_method(purpose)
