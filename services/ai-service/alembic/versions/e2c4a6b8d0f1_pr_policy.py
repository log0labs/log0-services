"""per-workspace pull request rules

Revision ID: e2c4a6b8d0f1
Revises: d1a3c5e7b9f4
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2c4a6b8d0f1"
down_revision: str | Sequence[str] | None = "d1a3c5e7b9f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"


def upgrade() -> None:
    op.create_table(
        "pr_policy",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("only_on_green", sa.Boolean(), nullable=False, server_default=sa.false()),
        # null: use the server's value. A workspace can only tighten what the server allows.
        sa.Column("max_files", sa.Integer(), nullable=True),
        sa.Column("max_changed_lines", sa.Integer(), nullable=True),
        sa.Column("max_per_repository_per_hour", sa.Integer(), nullable=True),
        sa.Column("max_per_tenant_per_hour", sa.Integer(), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "coalesce(max_files, 1) >= 1 AND coalesce(max_changed_lines, 1) >= 1 "
            "AND coalesce(max_per_repository_per_hour, 1) >= 1 AND coalesce(max_per_tenant_per_hour, 1) >= 1",
            name="ck_pr_policy_positive",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("pr_policy", schema=SCHEMA)
