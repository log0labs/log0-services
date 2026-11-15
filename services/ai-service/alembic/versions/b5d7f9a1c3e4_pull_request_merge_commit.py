"""a merged pull request remembers the commit its merge made

Revision ID: b5d7f9a1c3e4
Revises: a4c6e8b0d2f3
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5d7f9a1c3e4"
down_revision: str | Sequence[str] | None = "a4c6e8b0d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pull_request", sa.Column("merge_commit_sha", sa.String(length=64), nullable=True), schema="ai")
    op.add_column("pull_request", sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True), schema="ai")


def downgrade() -> None:
    op.drop_column("pull_request", "merged_at", schema="ai")
    op.drop_column("pull_request", "merge_commit_sha", schema="ai")
