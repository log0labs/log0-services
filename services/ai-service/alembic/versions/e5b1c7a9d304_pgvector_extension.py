"""enable the pgvector extension

Revision ID: e5b1c7a9d304
Revises: d7a3b9c15e20
Create Date: 2026-11-15

Runbook search and long-term memory (M5) store embeddings in the same Postgres as everything else.
The extension ships in the ``pgvector/pgvector:pg16`` image (same major version as ``postgres:16``,
so an existing data volume keeps working). Creating an extension needs a privileged role: the
compose database user is the superuser; on a managed Postgres, enable ``vector`` once from the
provider's console and this migration becomes a no-op (``IF NOT EXISTS``).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5b1c7a9d304"
down_revision: str | Sequence[str] | None = "d7a3b9c15e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
