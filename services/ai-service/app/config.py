"""Typed runtime configuration for the AI service.

Values come from environment variables (and a local ``.env`` file in dev).
"""

from functools import lru_cache
from typing import Annotated, Self

from pydantic import BeforeValidator, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.llm.model_spec import ModelSpec
from app.llm.providers import get_provider
from app.llm.purpose import Purpose, StructuredOutputMethod


def _coerce_model_spec(value: str | ModelSpec) -> ModelSpec:
    return ModelSpec.parse(value) if isinstance(value, str) else value


ModelSpecField = Annotated[ModelSpec, NoDecode, BeforeValidator(_coerce_model_spec)]


def _to_async_url(url: str) -> str:
    """Swap a sync Postgres driver prefix for the async psycopg one."""
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+psycopg_async://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg_async://", 1)
    return url


class Settings(BaseSettings):
    """All configuration the service needs, validated once at startup."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    service_name: str = "ai-service"
    database_url: str = "postgresql+psycopg://log0:log0@localhost:5433/log0"
    incident_service_url: str = "http://localhost:8083"
    internal_service_token: SecretStr | None = None
    jwt_secret: SecretStr | None = None
    
    # Per-purpose models: change any line to swap provider/model for testing.
    # Env names: LLM_SUMMARY, LLM_TRIAGE, LLM_INVESTIGATION, LLM_EVALUATION
    # Groq model ids change; run ``uv run python scripts/list_groq_models.py`` to refresh.
    llm_summary: ModelSpecField = ModelSpec(provider="groq", model="openai/gpt-oss-20b")
    llm_triage: ModelSpecField = ModelSpec(provider="groq", model="openai/gpt-oss-20b")
    llm_investigation: ModelSpecField = ModelSpec(
        provider="groq", model="qwen/qwen3.8-27b"
    )
    llm_evaluation: ModelSpecField = ModelSpec(
        provider="groq", model="openai/gpt-oss-120b"
    )
    
    # How each purpose asks its model for structured output. This depends on the MODEL, so change
    # it together with the model: Groq's gpt-oss models need json_schema (their tool calling
    # mangles the tool name); qwen on Groq rejects json_schema and needs function_calling.
    llm_triage_output_method: StructuredOutputMethod = "json_schema"
    llm_investigation_output_method: StructuredOutputMethod = "function_calling"

    # Generation defaults (per-purpose overrides can be added later if needed)
    llm_temperature: float = 0.2
    llm_summary_max_tokens: int = 300
    llm_default_max_tokens: int = 4096
    llm_timeout_seconds: int = 120

    # Platform API keys: set whichever providers you use (BYOK tenant keys come in M3)
    groq_api_key: SecretStr | None = None
    hf_token: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None

    # LangSmith (mirrored to os.environ via app.observability.configure_tracing)
    langchain_tracing_v2: bool = False
    langchain_api_key: SecretStr | None = None
    langchain_endpoint: str = "https://api.smith.langchain.com"
    langchain_project: str = "log0-ai-service"
    
    investigation_max_concurrent: int = 4
    # False runs the old no-op stub instead of the graph (kill switch; the tests use it too)
    investigation_graph_enabled: bool = True
    investigation_stream_poll_seconds: float = 0.5
    
    # ClickHouse (read-only ai_reader user)
    clickhouse_url: str = "http://localhost:8123"
    clickhouse_database: str = "log0"
    clickhouse_user: str = "ai_reader"
    clickhouse_password: SecretStr | None = None
    clickhouse_timeout_seconds: int = 15
    
    # Postgres public.incident, read through the SELECT-only ai_incident_reader role (2.4).
    # Host/port/database come from DATABASE_URL; only the credentials differ.
    incident_db_user: str = "ai_incident_reader"
    incident_db_password: SecretStr | None = None
    
    def structured_output_method(self, purpose: Purpose) -> StructuredOutputMethod:
        """The structured-output method for a purpose (LangChain's default for unconfigured ones)."""
        configured: dict[Purpose, StructuredOutputMethod] = {
            Purpose.TRIAGE: self.llm_triage_output_method,
            Purpose.INVESTIGATION: self.llm_investigation_output_method,
        }
        return configured.get(purpose, "function_calling")

    @property
    def llm_models(self) -> dict[Purpose, ModelSpec]:
        """Return a mapping of Purpose -> ModelSpec for the configured models."""
        return {
            Purpose.SUMMARY: self.llm_summary,
            Purpose.TRIAGE: self.llm_triage,
            Purpose.INVESTIGATION: self.llm_investigation,
            Purpose.EVALUATION: self.llm_evaluation,
        }
    
    @property
    def incident_database_url_async(self) -> str:
        """DATABASE_URL's host/port/db with the read-only reader credentials."""
        if self.incident_db_password is None:
            raise RuntimeError("INCIDENT_DB_PASSWORD is not set (see step 2.4-a)")
        url = make_url(self.database_url).set(
            username=self.incident_db_user,
            password=self.incident_db_password.get_secret_value(),
        )
        return _to_async_url(url.render_as_string(hide_password=False))

    @property
    def database_url_async(self) -> str:
        return _to_async_url(self.database_url)
    
    @model_validator(mode="after")
    def _require_keys_for_configured_models(self) -> Self:
        """Every configured purpose must have an API key for its provider."""
        missing: list[str] = []
        for purpose, spec in self.llm_models.items():
            get_provider(spec.provider)  # unknown provider → ValidationError
            key_name = get_provider(spec.provider).api_key_setting
            if getattr(self, key_name) is None:
                missing.append(f"{purpose.value} → {spec.format()} needs {key_name.upper()}")
        if missing:
            raise ValueError("Missing LLM API keys:\n  " + "\n  ".join(missing))
        return self
    
    @model_validator(mode="after")
    def _require_jwt_secret(self) -> Self:
        """Investigations verify JWT locally; secret must match auth-service."""
        if self.jwt_secret is None:
            raise ValueError(
                "JWT_SECRET is required (same value as auth-service/.env)"
            )
        raw = self.jwt_secret.get_secret_value()
        if len(raw) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters (HS256)")
        return self
    
    @model_validator(mode="after")
    def _require_internal_service_token(self) -> Self:
        if self.internal_service_token is None:
            raise ValueError("INTERNAL_SERVICE_TOKEN is required (shared with incident-service)")
        if len(self.internal_service_token.get_secret_value()) < 16:
            raise ValueError("INTERNAL_SERVICE_TOKEN must be at least 16 characters")
        return self

@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance (built on first call)."""
    return Settings()
