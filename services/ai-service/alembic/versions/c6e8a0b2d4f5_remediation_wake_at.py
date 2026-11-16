"""a waiting remediation records when it wants to be looked at again

Revision ID: c6e8a0b2d4f5
Revises: b5d7f9a1c3e4
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6e8a0b2d4f5"
down_revision: str | Sequence[str] | None = "b5d7f9a1c3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("remediation", sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True), schema="ai")
    op.create_index("ix_remediation_wake", "remediation", ["wake_at"], schema="ai", postgresql_where=sa.text("wake_at IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_remediation_wake", table_name="remediation", schema="ai")
    op.drop_column("remediation", "wake_at")
