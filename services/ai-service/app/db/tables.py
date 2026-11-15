"""SQLAlchemy Core tables for the ai schema (mirrors Alembic migration)."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.schemas.remediation import (
    ACTIVE_STATUSES,
    DEPLOYMENT_STATUSES,
    GATES,
    KINDS,
    RELEASE_SOURCES,
    STATUSES,
    VERDICTS,
)

metadata = MetaData(schema="ai")

investigation = Table(
    "investigation",
    metadata,
    Column(
        "id",
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    ),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("incident_id", PGUUID(as_uuid=True), nullable=False),
    Column("requested_by", PGUUID(as_uuid=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("question", Text(), nullable=True),
    Column(
        "scope",
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    Column("provider", String(64), nullable=True),
    Column("model", String(128), nullable=True),
    Column("prompt_version", String(64), nullable=True),
    Column("graph_version", String(64), nullable=True),
    Column("repo_sha", String(64), nullable=True),
    Column("root_cause", JSONB(astext_type=Text()), nullable=True),
    Column("recommendation", JSONB(astext_type=Text()), nullable=True),
    Column("diff", Text(), nullable=True),
    Column("confidence_band", String(32), nullable=True),
    Column("error", Text(), nullable=True),
    Column("input_tokens", Integer(), nullable=True),
    Column("output_tokens", Integer(), nullable=True),
    Column("cost_usd", Numeric(12, 6), nullable=True),
    Column("tool_call_count", Integer(), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
)

investigation_event = Table(
    "investigation_event",
    metadata,
    Column(
        "investigation_id",
        PGUUID(as_uuid=True),
        ForeignKey("ai.investigation.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("seq", BigInteger(), primary_key=True),
    Column("type", String(128), nullable=False),
    Column(
        "payload",
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    ),
)

# ---------------------------------------------------------------------------- M3
# Constraints (checks, uniques, the composite foreign key) live in the Alembic migration;
# these definitions only need the columns and primary keys for building queries.

provider_config = Table(
    "provider_config",
    metadata,
    Column("tenant_id", PGUUID(as_uuid=True), primary_key=True),
    Column("purpose", String(32), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("base_url", Text(), nullable=True),
    Column("api_key_ciphertext", Text(), nullable=True),
    Column("key_id", String(64), nullable=True),
    Column("key_last4", String(8), nullable=True),
    Column("temperature", Numeric(3, 2), nullable=True),
    Column("max_tokens", Integer(), nullable=True),
    Column("updated_by", PGUUID(as_uuid=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
)

repository_connection = Table(
    "repository_connection",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("provider", String(32), nullable=False, server_default="github"),
    Column("owner", String(128), nullable=False),
    Column("name", String(128), nullable=False),
    Column("default_branch", String(255), nullable=False, server_default="main"),
    Column("token_ciphertext", Text(), nullable=False),
    Column("key_id", String(64), nullable=False),
    Column("created_by", PGUUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Column("github_repository_id", BigInteger(), nullable=True),
    Column(
        "installation_id",
        BigInteger(),
        ForeignKey("ai.github_installation.installation_id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("pr_writes_enabled", Boolean(), nullable=False, server_default=false()),
)

service_repository = Table(
    "service_repository",
    metadata,
    Column("tenant_id", PGUUID(as_uuid=True), primary_key=True),
    Column("service_name", String(255), primary_key=True),
    Column("repository_id", PGUUID(as_uuid=True), nullable=False),
    Column("path_prefix", Text(), nullable=False, server_default=""),
)

github_installation = Table(
    "github_installation",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("installation_id", BigInteger(), nullable=False, unique=True),
    Column("account_login", String(128), nullable=False),
    Column("account_type", String(32), nullable=False),
    Column("suspended_at", DateTime(timezone=True), nullable=True),
    Column("created_by", PGUUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
)

pull_request = Table(
    "pull_request",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column(
        "investigation_id",
        PGUUID(as_uuid=True),
        ForeignKey("ai.investigation.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column(
        "repository_id",
        PGUUID(as_uuid=True),
        ForeignKey("ai.repository_connection.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("branch", String(255), nullable=False),
    Column("base_sha", String(64), nullable=False),
    Column("commit_sha", String(64), nullable=True),
    Column("number", Integer(), nullable=True),
    Column("url", Text(), nullable=True),
    Column("state", String(24), nullable=False, server_default="branch_created"),
    Column("approved_by", PGUUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Column("merge_commit_sha", String(64), nullable=True),
    Column("merged_at", DateTime(timezone=True), nullable=True),
)

test_run = Table(
    "test_run",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column(
        "investigation_id",
        PGUUID(as_uuid=True),
        ForeignKey("ai.investigation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("commit_sha", String(64), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("checks", JSONB(astext_type=Text()), nullable=False, server_default=text("'[]'::jsonb")),
    Column("started_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
)

write_audit = Table(
    "write_audit",
    metadata,
    Column("id", BigInteger(), primary_key=True, autoincrement=True),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("investigation_id", PGUUID(as_uuid=True), nullable=True),
    Column("actor_id", PGUUID(as_uuid=True), nullable=True),
    Column("action", String(48), nullable=False),
    Column("repository", String(300), nullable=False),
    Column("target", Text(), nullable=False, server_default=""),
    Column("result", String(16), nullable=False),
    Column("detail", Text(), nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
)

pr_policy = Table(
    "pr_policy",
    metadata,
    Column("tenant_id", PGUUID(as_uuid=True), primary_key=True),
    Column("only_on_green", Boolean(), nullable=False, server_default=false()),
    Column("sandbox_tests", Boolean(), nullable=False, server_default=false()),
    Column("max_files", Integer(), nullable=True),
    Column("max_changed_lines", Integer(), nullable=True),
    Column("max_per_repository_per_hour", Integer(), nullable=True),
    Column("max_per_tenant_per_hour", Integer(), nullable=True),
    Column("updated_by", PGUUID(as_uuid=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
)


def _in_list(column: str, values: tuple[str, ...] | frozenset[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in sorted(values))})"


deploy_target = Table(
    "deploy_target",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("repository_id", PGUUID(as_uuid=True), ForeignKey("ai.repository_connection.id", ondelete="CASCADE"), nullable=False),
    Column("workflow_file", String(255), nullable=False),
    Column("environments", JSONB(), nullable=False, server_default=text("'[]'::jsonb")),
    Column("updated_by", PGUUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    UniqueConstraint("repository_id", name="uq_deploy_target_repository"),
)

remediation = Table(
    "remediation",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("pull_request_id", PGUUID(as_uuid=True), ForeignKey("ai.pull_request.id", ondelete="CASCADE"), nullable=False),
    Column("investigation_id", PGUUID(as_uuid=True), ForeignKey("ai.investigation.id", ondelete="CASCADE"), nullable=False),
    Column("repository_id", PGUUID(as_uuid=True), ForeignKey("ai.repository_connection.id", ondelete="CASCADE"), nullable=False),
    Column("environment", String(64), nullable=False),
    Column("merge_sha", String(64), nullable=False),
    Column("rollback_target", String(64), nullable=True),
    Column("status", String(32), nullable=False, server_default="OFFERED"),
    Column("baseline", JSONB(), nullable=True),
    Column("policy", JSONB(), nullable=True),
    Column("outcome", JSONB(), nullable=True),
    Column("started_by", PGUUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("wake_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("pull_request_id", "environment", name="uq_remediation_pull_request_environment"),
    CheckConstraint(_in_list("status", STATUSES), name="ck_remediation_status"),
    Index(
        "uq_remediation_active_environment",
        "repository_id",
        "environment",
        unique=True,
        postgresql_where=text(_in_list("status", ACTIVE_STATUSES)),
    ),
    Index("ix_remediation_tenant", "tenant_id", "created_at"),
    Index("ix_remediation_wake", "wake_at", postgresql_where=text("wake_at IS NOT NULL")),
)

deployment = Table(
    "deployment",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("remediation_id", PGUUID(as_uuid=True), ForeignKey("ai.remediation.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(12), nullable=False),
    Column("environment", String(64), nullable=False),
    Column("sha", String(64), nullable=False),
    Column("correlation_id", PGUUID(as_uuid=True), nullable=False),
    Column("run_id", BigInteger(), nullable=True),
    Column("run_url", Text(), nullable=True),
    Column("status", String(16), nullable=False, server_default="dispatched"),
    Column("dispatched_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("correlation_id", name="uq_deployment_correlation"),
    UniqueConstraint("remediation_id", "kind", name="uq_deployment_remediation_kind"),
    CheckConstraint(_in_list("kind", KINDS), name="ck_deployment_kind"),
    CheckConstraint(_in_list("status", DEPLOYMENT_STATUSES), name="ck_deployment_status"),
)

verification = Table(
    "verification",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("remediation_id", PGUUID(as_uuid=True), ForeignKey("ai.remediation.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(12), nullable=False),
    Column("verdict", String(16), nullable=False),
    Column("numbers", JSONB(), nullable=False, server_default=text("'{}'::jsonb")),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    CheckConstraint(_in_list("kind", KINDS), name="ck_verification_kind"),
    CheckConstraint(_in_list("verdict", VERDICTS), name="ck_verification_verdict"),
    Index("ix_verification_remediation", "remediation_id", "created_at"),
)

approval = Table(
    "approval",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("remediation_id", PGUUID(as_uuid=True), ForeignKey("ai.remediation.id", ondelete="CASCADE"), nullable=False),
    Column("gate", String(12), nullable=False),
    Column("decision", String(12), nullable=False),
    Column("user_id", PGUUID(as_uuid=True), nullable=False),
    Column("role", String(16), nullable=False),
    Column("environment", String(64), nullable=False),
    Column("sha", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    CheckConstraint(_in_list("gate", GATES), name="ck_approval_gate"),
    Index("ix_approval_remediation", "remediation_id", "created_at"),
)

release_ledger = Table(
    "release_ledger",
    metadata,
    Column("id", BigInteger(), Identity(), primary_key=True),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("repository_id", PGUUID(as_uuid=True), ForeignKey("ai.repository_connection.id", ondelete="CASCADE"), nullable=False),
    Column("environment", String(64), nullable=False),
    Column("sha", String(64), nullable=False),
    Column("source", String(12), nullable=False),
    Column("remediation_id", PGUUID(as_uuid=True), ForeignKey("ai.remediation.id", ondelete="SET NULL"), nullable=True),
    Column("recorded_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    CheckConstraint(_in_list("source", RELEASE_SOURCES), name="ck_release_ledger_source"),
    Index("ix_release_ledger_environment", "repository_id", "environment", "recorded_at"),
)

deploy_audit = Table(
    "deploy_audit",
    metadata,
    Column("id", BigInteger(), Identity(), primary_key=True),
    Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
    Column("remediation_id", PGUUID(as_uuid=True), nullable=True),
    Column("actor_id", PGUUID(as_uuid=True), nullable=True),
    Column("action", String(48), nullable=False),
    Column("environment", String(64), nullable=False, server_default=""),
    Column("sha", String(64), nullable=False, server_default=""),
    Column("result", String(16), nullable=False),
    Column("detail", Text(), nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), server_default=text("now()"), nullable=False),
    Index("ix_deploy_audit_tenant", "tenant_id", "created_at"),
)
