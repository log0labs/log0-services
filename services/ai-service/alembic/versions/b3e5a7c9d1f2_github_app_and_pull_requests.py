"""GitHub App installations, pull requests, test runs and the write audit (V2)

Revision ID: b3e5a7c9d1f2
Revises: a2f4c6e8b1d3
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3e5a7c9d1f2"
down_revision: str | Sequence[str] | None = "a2f4c6e8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "github_installation",
        _uuid_pk(),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(length=128), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        _created_at(),
        # one GitHub installation belongs to exactly one tenant
        sa.UniqueConstraint("installation_id", name="uq_github_installation_installation_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_github_installation_tenant", "github_installation", ["tenant_id"], schema=SCHEMA)

    op.add_column("repository_connection", sa.Column("github_repository_id", sa.BigInteger(), nullable=True), schema=SCHEMA)
    op.add_column("repository_connection", sa.Column("installation_id", sa.BigInteger(), nullable=True), schema=SCHEMA)
    op.add_column(
        "repository_connection",
        sa.Column("pr_writes_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_repository_connection_installation",
        "repository_connection",
        "github_installation",
        ["installation_id"],
        ["installation_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )

    op.create_table(
        "pull_request",
        _uuid_pk(),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        # branch_created, committed, open (a draft), merged, closed
        sa.Column("state", sa.String(length=24), nullable=False, server_default="branch_created"),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=False),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["investigation_id"], [f"{SCHEMA}.investigation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], [f"{SCHEMA}.repository_connection.id"], ondelete="CASCADE"),
        # one pull request per investigation: a second approve returns the first
        sa.UniqueConstraint("investigation_id", name="uq_pull_request_investigation"),
        schema=SCHEMA,
    )
    op.create_index("ix_pull_request_tenant", "pull_request", ["tenant_id", "created_at"], schema=SCHEMA)

    op.create_table(
        "test_run",
        _uuid_pk(),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        # pending, passed, failed, none (no CI on this repository), timeout
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("checks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["investigation_id"], [f"{SCHEMA}.investigation.id"], ondelete="CASCADE"),
        schema=SCHEMA,
    )
    op.create_index("ix_test_run_investigation", "test_run", ["investigation_id", "started_at"], schema=SCHEMA)

    op.create_table(
        "write_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("repository", sa.String(length=300), nullable=False),
        sa.Column("target", sa.Text(), nullable=False, server_default=""),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        _created_at(),
        schema=SCHEMA,
    )
    op.create_index("ix_write_audit_tenant", "write_audit", ["tenant_id", "created_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("write_audit", schema=SCHEMA)
    op.drop_table("test_run", schema=SCHEMA)
    op.drop_table("pull_request", schema=SCHEMA)
    op.drop_constraint("fk_repository_connection_installation", "repository_connection", schema=SCHEMA, type_="foreignkey")
    op.drop_column("repository_connection", "pr_writes_enabled", schema=SCHEMA)
    op.drop_column("repository_connection", "installation_id", schema=SCHEMA)
    op.drop_column("repository_connection", "github_repository_id", schema=SCHEMA)
    op.drop_table("github_installation", schema=SCHEMA)
