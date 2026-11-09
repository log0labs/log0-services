"""One object that hands out a ready chat model per purpose.

Graph nodes should not know about providers, API keys or settings. They ask
``runtime.context.models.for_purpose(Purpose.TRIAGE)`` and get a LangChain chat model.
Tests pass a fake with the same method, so no node test ever touches the network.
"""

from langchain_core.language_models import BaseChatModel

from app.config import Settings
from app.llm.factory import chat_model_for_purpose
from app.llm.generation import generation_for_purpose
from app.llm.purpose import Purpose, StructuredOutputMethod
from app.llm.resolver import LlmResolver, PlatformLlmResolver


class PurposeChatModels:
    """Builds the chat model for a purpose on first use and reuses it afterwards.

    A chat model holds an HTTP client and is safe to share between investigations, so
    building one per call would only waste time. The cache is filled lazily, which also
    means a missing API key for a purpose nobody uses never raises.
    """

    def __init__(self, settings: Settings, resolver: LlmResolver | None = None) -> None:
        self._settings = settings
        self._resolver = resolver or PlatformLlmResolver(settings)
        self._cache: dict[Purpose, BaseChatModel] = {}
    
    def for_purpose(self, purpose: Purpose) -> BaseChatModel:
        model = self._cache.get(purpose)
        if model is None:
            model = chat_model_for_purpose(
                self._resolver, purpose,
                generation_for_purpose(self._settings, purpose)
            )
            self._cache[purpose] = model
        return model
    
    def structured_method(self, purpose: Purpose) -> StructuredOutputMethod:
        """How to ask this purpose's model for structured output (it depends on the model)."""
        return self._settings.structured_output_method(purpose)
