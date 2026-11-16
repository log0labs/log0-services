from app.config import Settings
from app.llm.factory import GenerationConfig
from app.llm.purpose import Purpose


def generation_for_purpose(settings: Settings, purpose: Purpose) -> GenerationConfig:
    """Return a GenerationConfig for the given purpose, using the service settings."""
    max_tokens = (
        settings.llm_summary_max_tokens
        if purpose == Purpose.SUMMARY
        else settings.llm_default_max_tokens
    )
    # Triage is a classification: the same facts should give the same routing.
    temperature = 0.0 if purpose == Purpose.TRIAGE else settings.llm_temperature
    return GenerationConfig(
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
    )

