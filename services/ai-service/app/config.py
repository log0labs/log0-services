"""Typed runtime configuration for the AI service.

Values come from environment variables (and a local ``.env`` file in dev).
"""

from functools import lru_cache
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BeforeValidator, Field, SecretStr, model_validator
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
    # An optional stronger model for the two steps that decide the outcome (root cause and writing the fix); unset, it uses the investigation model.
    # A workspace that chose its own investigation model keeps it. Env name: LLM_ROOT_CAUSE
    llm_root_cause: ModelSpecField | None = None
    llm_evaluation: ModelSpecField = ModelSpec(
        provider="groq", model="openai/gpt-oss-120b"
    )
    
    # How each purpose asks its model for structured output. This depends on the MODEL, so change
    # it together with the model: Groq's gpt-oss models need json_schema (their tool calling
    # mangles the tool name); qwen on Groq rejects json_schema and needs function_calling.
    llm_triage_output_method: StructuredOutputMethod = "json_schema"
    llm_investigation_output_method: StructuredOutputMethod = "function_calling"

    # What a model costs, USD per million (input, output) tokens, by the model name the provider
    # reports, e.g. LLM_PRICES='{"openai/gpt-oss-120b": [0.15, 0.60]}'. Empty by default: a price
    # I am not given is not guessed, so an unpriced model leaves cost_usd empty.
    llm_prices: dict[str, tuple[float, float]] = Field(default_factory=dict)

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

    # Embeddings for runbook search and memory (M5). "hash" is built in: no network, no model, it
    # matches shared words only (fine for development and tests). "huggingface" calls the HF
    # inference router with HF_TOKEN and gives real semantic search; the model must produce
    # vectors of EMBEDDING_DIMENSION numbers (384 for sentence-transformers/all-MiniLM-L6-v2).
    # The database column has a fixed size, so changing the model to another size needs a migration.
    embedding_provider: Literal["hash", "huggingface"] = "hash"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    hf_embedding_base_url: str = "https://router.huggingface.co/hf-inference"
    embedding_timeout_seconds: float = 30.0

    # Serve log0's read-only tools to MCP clients at /mcp (streamable HTTP, bearer-token protected). Off by
    # default: switch it on where you want Claude Desktop, Cursor or another MCP client to reach this service.
    mcp_http_enabled: bool = False

    # LangSmith (mirrored to os.environ via app.observability.configure_tracing)
    langchain_tracing_v2: bool = False
    langchain_api_key: SecretStr | None = None
    langchain_endpoint: str = "https://api.smith.langchain.com"
    langchain_project: str = "log0-ai-service"
    # Tenants that brought their own LLM key (BYOK) are traced only if they opted in, because a trace holds the
    # prompts and answers of their incidents and goes to a third party. List their ids here to opt them in, e.g.
    # TRACING_OPT_IN_TENANTS='["11111111-1111-4111-8111-111111111111"]'. Tenants on the platform's own keys are
    # traced whenever LANGCHAIN_TRACING_V2 is on.
    tracing_opt_in_tenants: set[UUID] = Field(default_factory=set)
    
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
    
    # Master keys that encrypt tenant secrets (LLM API keys, repository tokens) before they are
    # stored. A JSON object of key id -> Fernet key, e.g. SECRETS_KEYS='{"k1": "<key>"}'; make a
    # key with `uv run python scripts/new_secret_key.py`. New secrets use SECRETS_ACTIVE_KEY_ID;
    # old ones stay readable while their key id stays in SECRETS_KEYS. Empty by default: the
    # service starts without them and only refuses to store or read a tenant secret.
    secrets_keys: dict[str, SecretStr] = Field(default_factory=dict)
    secrets_active_key_id: str = ""

    # GitHub's REST API, used to check a repository token when a tenant connects a repository.
    github_api_url: str = "https://api.github.com"
    github_timeout_seconds: float = 10.0

    # The log0 GitHub App (V2): the only thing that may write to a customer repository. It signs a
    # short-lived JWT with its private key (PEM; "\n" escapes are accepted so it fits in one
    # env line) and swaps it for one-hour installation tokens. Without these the service runs as
    # in V1 and V2 steps report "GitHub App is not configured".
    github_app_id: str = ""
    github_app_private_key: SecretStr | None = None
    github_app_slug: str = ""  # the app's URL name: https://github.com/apps/<slug>/installations/new
    github_webhook_secret: SecretStr | None = None
    # Global kill switch for every GitHub write (branch, commit, pull request).
    pr_writes_enabled: bool = False
    # How long a run waits for the repository's own CI on the commit it made, before opening the draft pull request.
    ci_wait_seconds: int = 600
    ci_poll_seconds: float = 15.0
    ci_repair_attempts: int = 1  # how many times log0 may fix its change after the repository's checks fail (0: never)
    ci_start_grace_seconds: int = 60  # no check at all after this long means the repository has no CI for the commit
    # Workspaces whose pull request writes are switched off by the operator (tenant ids). Takes effect on the next write.
    pr_writes_disabled_tenants: set[UUID] = Field(default_factory=set)  # JSON in the environment: ["<tenant id>"]
    # Remediation (V3): deploying a merged fix. Off until an operator turns it on; a workspace is switched off by listing it.
    deploys_enabled: bool = False
    deploys_disabled_tenants: set[UUID] = Field(default_factory=set)
    deploy_max_per_environment_per_hour: int = 3
    deploy_poll_seconds: float = 30.0  # how often a waiting deploy looks at its pipeline run
    deploy_sweep_seconds: float = 10.0  # how often the sweeper looks for runs whose wake-up time has come
    # What a pull request may be: no protected files (workflows, locks, keys), at most this much, at most this often.
    pr_max_files: int = 25  # the most a pull request log0 opens may touch: a human has to review it, and review quality falls with size
    pr_max_changed_lines: int = 1000
    pr_max_per_repository_per_hour: int = 3
    pr_max_per_tenant_per_hour: int = 10
    # Optional: run a repository's own tests in a locked-down container when it has no CI (see app/sandbox/docker.py).
    # Off by default. Point SANDBOX_DOCKER_HOST at a dedicated runner daemon; never mount the host Docker socket.
    sandbox_enabled: bool = False
    sandbox_docker_host: str = ""
    sandbox_runtime: str = ""  # "runsc" for gVisor
    sandbox_prepare_network: str = ""  # egress-restricted network for the dependency step
    sandbox_prepare_proxy: str = ""
    sandbox_prepare_open: bool = False  # development only: an unrestricted network for the dependency step
    sandbox_images: dict[str, str] = Field(default_factory=dict)  # override go, node, rust, python images
    sandbox_memory: str = "1g"
    sandbox_cpus: str = "1.0"
    sandbox_pids: int = 256
    sandbox_test_timeout_seconds: float = 300.0
    sandbox_prepare_timeout_seconds: float = 300.0
    sandbox_max_workdir_mb: int = 700
    pr_only_on_green: bool = False  # True: do not open the pull request when the checks failed or timed out

    # Local copies of tenants' repositories for the code tools (3.4). The cache is disposable:
    # deleting the folder only means the next investigation clones again.
    repo_cache_dir: str = ".repo-cache"
    repo_clone_base_url: str = "https://github.com"  # clone URL = <base>/<owner>/<name>.git
    git_binary: str = "git"
    rg_binary: str = "rg"  # ripgrep, for the code search tool

    # Pause every investigation that reaches a root cause for an engineer's decision (approve, refine
    # or stop). Off by default: it only helps once something in the console can answer (step 4.10),
    # and a waiting investigation blocks a new one for the same incident until it is answered.
    investigation_review_gate: bool = False
    git_timeout_seconds: float = 120
    git_allowed_protocols: str = "https"  # GIT_ALLOW_PROTOCOL; tests add "file" to clone from a local folder
    repo_fresh_seconds: float = 60  # a checkout synced more recently than this is reused without fetching
    repo_history_depth: int = 300  # commits kept per checkout: what recent_commits, blame and show_commit can see

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
    def _require_token_for_hf_embeddings(self) -> Self:
        if self.embedding_provider == "huggingface" and self.hf_token is None:
            raise ValueError("EMBEDDING_PROVIDER=huggingface needs HF_TOKEN")
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
