"""scope the one-active-investigation index to the tenant

Revision ID: c4d8e2f1a6b7
Revises: a9064de27579
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4d8e2f1a6b7"
down_revision: str | Sequence[str] | None = "a9064de27579"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"
INDEX = "uq_investigation_one_active_per_incident"
_ACTIVE = "status IN ('QUEUED', 'RUNNING', 'AWAITING_REVIEW')"


def upgrade() -> None:
    op.drop_index(INDEX, table_name="investigation", schema=SCHEMA)
    op.create_index(
        INDEX,
        "investigation",
        ["tenant_id", "incident_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(_ACTIVE),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="investigation", schema=SCHEMA)
    op.create_index(
        INDEX,
        "investigation",
        ["incident_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(_ACTIVE),
    )
