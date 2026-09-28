"""Typed runtime configuration for the AI service.

Values come from environment variables (and a local ``.env`` file in dev).
Defaults mirror the former Java ``application.yml`` so behaviour is unchanged.
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration the service needs, validated once at startup."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    service_name: str = "ai-service"
    incident_service_url: str = "http://localhost:8083"
    
    llm_provider: str = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 300
    llm_timeout_seconds: int = 30
    
    groq_api_key: SecretStr


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance (built on first call)."""
    return Settings()
