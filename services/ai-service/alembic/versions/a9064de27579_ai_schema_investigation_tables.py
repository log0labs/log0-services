"""ai schema: investigation and investigation_event

Revision ID: (alembic fills)
Revises:
Create Date: (alembic fills)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9064de27579"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "ai"

_ACTIVE_STATUSES = ("QUEUED", "RUNNING", "AWAITING_REVIEW")


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))

    op.create_table(
        "investigation",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("graph_version", sa.String(length=64), nullable=True),
        sa.Column("repo_sha", sa.String(length=64), nullable=True),
        sa.Column("root_cause", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recommendation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("diff", sa.Text(), nullable=True),
        sa.Column("confidence_band", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("tool_call_count", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_investigation_tenant_incident",
        "investigation",
        ["tenant_id", "incident_id"],
        schema=SCHEMA,
    )

    status_list = ", ".join(f"'{s}'" for s in _ACTIVE_STATUSES)
    op.create_index(
        "uq_investigation_one_active_per_incident",
        "investigation",
        ["incident_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(f"status IN ({status_list})"),
    )

    op.create_table(
        "investigation_event",
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.investigation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("investigation_id", "seq", name="pk_investigation_event"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("investigation_event", schema=SCHEMA)
    op.drop_index(
        "uq_investigation_one_active_per_incident",
        table_name="investigation",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_investigation_tenant_incident",
        table_name="investigation",
        schema=SCHEMA,
    )
    op.drop_table("investigation", schema=SCHEMA)
    # Leaves ai.alembic_version; next upgrade still works. To wipe fully: DROP SCHEMA ai CASCADE (manual).
