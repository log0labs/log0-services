"""SQLAlchemy Core tables for the ai schema (mirrors Alembic migration)."""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

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
)

service_repository = Table(
    "service_repository",
    metadata,
    Column("tenant_id", PGUUID(as_uuid=True), primary_key=True),
    Column("service_name", String(255), primary_key=True),
    Column("repository_id", PGUUID(as_uuid=True), nullable=False),
    Column("path_prefix", Text(), nullable=False, server_default=""),
)
