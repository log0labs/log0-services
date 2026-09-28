"""Registry: log0 provider name → how LangChain reaches it + which Settings key holds the API key."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    langchain_provider: str
    api_key_setting: str
    base_url: str | None = None


PROVIDERS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec(langchain_provider="groq", api_key_setting="groq_api_key"),
    "openai": ProviderSpec(langchain_provider="openai", api_key_setting="openai_api_key"),
    "anthropic": ProviderSpec(langchain_provider="anthropic", api_key_setting="anthropic_api_key"),
    "google_genai": ProviderSpec(
        langchain_provider="google_genai",
        api_key_setting="google_api_key",
    ),
    "huggingface": ProviderSpec(
        langchain_provider="openai",
        api_key_setting="hf_token",
        base_url="https://router.huggingface.co/v1",
    ),
    "openrouter": ProviderSpec(
        langchain_provider="openai",
        api_key_setting="openrouter_api_key",
        base_url="https://openrouter.ai/api/v1",
    ),
}


def get_provider(name: str) -> ProviderSpec:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"Unknown LLM provider {name!r}; expected one of {sorted(PROVIDERS)}"
        ) from None
