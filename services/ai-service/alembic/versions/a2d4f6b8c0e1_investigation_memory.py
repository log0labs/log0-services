"""long-term memory of finished investigations

Revision ID: a2d4f6b8c0e1
Revises: f1c9d3e7a2b8
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2d4f6b8c0e1"
down_revision: str | Sequence[str] | None = "f1c9d3e7a2b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"
DIMENSION = 384  # app.retrieval.embeddings.EMBEDDING_DIMENSION
VERDICTS = ("helpful", "partly", "wrong")


def upgrade() -> None:
    op.create_table(
        "investigation_memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # one memory per investigation; it disappears with the investigation
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_name", sa.Text(), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("confidence_band", sa.String(length=16), nullable=True),
        # what the engineer said about the result: it decides whether this memory is trusted later
        sa.Column("feedback_verdict", sa.String(length=16), nullable=True),
        sa.Column("feedback_note", sa.Text(), nullable=True),
        sa.Column("feedback_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_memory"),
        sa.UniqueConstraint("investigation_id", name="uq_investigation_memory_investigation"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], [f"{SCHEMA}.investigation.id"], name="fk_investigation_memory_investigation", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "feedback_verdict IS NULL OR feedback_verdict IN (" + ", ".join(repr(v) for v in VERDICTS) + ")",
            name="ck_investigation_memory_verdict",
        ),
        schema=SCHEMA,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.investigation_memory ADD COLUMN embedding vector({DIMENSION}) NOT NULL")
    op.create_index("ix_investigation_memory_tenant", "investigation_memory", ["tenant_id", "created_at"], schema=SCHEMA)
    op.execute(
        f"CREATE INDEX ix_investigation_memory_embedding ON {SCHEMA}.investigation_memory "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_table("investigation_memory", schema=SCHEMA)
