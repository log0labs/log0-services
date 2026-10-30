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
