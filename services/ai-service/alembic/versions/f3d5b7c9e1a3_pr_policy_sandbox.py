"""a workspace can opt in to running tests in the sandbox

Revision ID: f3d5b7c9e1a3
Revises: e2c4a6b8d0f1
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3d5b7c9e1a3"
down_revision: str | Sequence[str] | None = "e2c4a6b8d0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pr_policy", sa.Column("sandbox_tests", sa.Boolean(), nullable=False, server_default=sa.false()), schema="ai")


def downgrade() -> None:
    op.drop_column("pr_policy", "sandbox_tests", schema="ai")
