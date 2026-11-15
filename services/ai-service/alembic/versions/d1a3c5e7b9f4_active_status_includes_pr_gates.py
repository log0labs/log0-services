"""the one-active-investigation index also covers the pull-request gate and publishing

Revision ID: d1a3c5e7b9f4
Revises: b3e5a7c9d1f2
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1a3c5e7b9f4"
down_revision: str | Sequence[str] | None = "b3e5a7c9d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"
INDEX = "uq_investigation_one_active_per_incident"
_BEFORE = "status IN ('QUEUED', 'RUNNING', 'AWAITING_REVIEW')"
_AFTER = "status IN ('QUEUED', 'RUNNING', 'AWAITING_REVIEW', 'AWAITING_PR_APPROVAL', 'PUBLISHING')"


def _recreate(where: str) -> None:
    op.drop_index(INDEX, table_name="investigation", schema=SCHEMA)
    op.create_index(
        INDEX,
        "investigation",
        ["tenant_id", "incident_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(where),
    )


def upgrade() -> None:
    _recreate(_AFTER)


def downgrade() -> None:
    op.execute(
        "UPDATE ai.investigation SET status = 'COMPLETED' WHERE status IN ('AWAITING_PR_APPROVAL', 'PUBLISHING')"
    )
    _recreate(_BEFORE)
