"""keep the original of each runbook so it can be shown and downloaded

Revision ID: a2f4c6e8b1d3
Revises: a2d4f6b8c0e1
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2f4c6e8b1d3"
down_revision: str | Sequence[str] | None = "a2d4f6b8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"


def upgrade() -> None:
    # 'markdown' or 'text' (the text as uploaded), or 'pdf' or 'docx' (the file as uploaded; the indexed text was extracted from it)
    op.add_column("runbook", sa.Column("source_kind", sa.String(length=16), server_default="markdown", nullable=False), schema=SCHEMA)
    # the uploaded Markdown, or the text extracted from a PDF; null for runbooks stored before this column existed
    op.add_column("runbook", sa.Column("source_text", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column("runbook", sa.Column("source_file", sa.LargeBinary(), nullable=True), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("runbook", "source_file", schema=SCHEMA)
    op.drop_column("runbook", "source_text", schema=SCHEMA)
    op.drop_column("runbook", "source_kind", schema=SCHEMA)
