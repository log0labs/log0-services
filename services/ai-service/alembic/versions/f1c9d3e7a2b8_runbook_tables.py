"""runbooks and their searchable chunks

Revision ID: f1c9d3e7a2b8
Revises: e5b1c7a9d304
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f1c9d3e7a2b8"
down_revision: str | Sequence[str] | None = "e5b1c7a9d304"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"
DIMENSION = 384  # app.retrieval.embeddings.EMBEDDING_DIMENSION: a different model size needs a new migration


def upgrade() -> None:
    op.create_table(
        "runbook",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        # sha-256 of the Markdown: uploading the same text again does not call the embedding service again
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        # the model that made the vectors: vectors of different models cannot be compared
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runbook"),
        sa.UniqueConstraint("tenant_id", "title", name="uq_runbook_tenant_title"),
        schema=SCHEMA,
    )
    op.create_table(
        "runbook_chunk",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("runbook_id", postgresql.UUID(as_uuid=True), nullable=False),
        # copied from the runbook so a search filters on one table (and a bug cannot widen it)
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runbook_chunk"),
        sa.ForeignKeyConstraint(
            ["runbook_id"], [f"{SCHEMA}.runbook.id"], name="fk_runbook_chunk_runbook", ondelete="CASCADE"
        ),
        schema=SCHEMA,
    )
    # the vector column: a type SQLAlchemy does not know, so it is added in SQL
    op.execute(f"ALTER TABLE {SCHEMA}.runbook_chunk ADD COLUMN embedding vector({DIMENSION}) NOT NULL")
    op.create_index("ix_runbook_chunk_tenant", "runbook_chunk", ["tenant_id"], schema=SCHEMA)
    op.create_index("ix_runbook_chunk_runbook", "runbook_chunk", ["runbook_id", "ordinal"], schema=SCHEMA)
    op.execute(
        f"CREATE INDEX ix_runbook_chunk_embedding ON {SCHEMA}.runbook_chunk USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_table("runbook_chunk", schema=SCHEMA)
    op.drop_table("runbook", schema=SCHEMA)
