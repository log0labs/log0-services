"""remediation: deploy targets, remediations, deployments, verifications, approvals, release ledger, append-only audit (V3)

Revision ID: a4c6e8b0d2f3
Revises: f3d5b7c9e1a3
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a4c6e8b0d2f3"
down_revision: str | Sequence[str] | None = "f3d5b7c9e1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"

STATUSES = (
    "ABANDONED",
    "AWAITING_DECISION",
    "AWAITING_DEPLOY_APPROVAL",
    "AWAITING_ROLLBACK_APPROVAL",
    "CANCELLED",
    "DECLINED",
    "DEPLOYING",
    "FAILED",
    "OFFERED",
    "PREFLIGHT",
    "ROLLED_BACK",
    "ROLLING_BACK",
    "VERIFIED",
    "VERIFYING",
)
ACTIVE = (
    "AWAITING_DECISION",
    "AWAITING_DEPLOY_APPROVAL",
    "AWAITING_ROLLBACK_APPROVAL",
    "DEPLOYING",
    "PREFLIGHT",
    "ROLLING_BACK",
    "VERIFYING",
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.text("now()")
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "deploy_target",
        _uuid_pk(),
        _uuid("tenant_id"),
        _uuid("repository_id"),
        sa.Column("workflow_file", sa.String(length=255), nullable=False),
        sa.Column("environments", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _uuid("updated_by"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.ForeignKeyConstraint(["repository_id"], [f"{SCHEMA}.repository_connection.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("repository_id", name="uq_deploy_target_repository"),
        schema=SCHEMA,
    )

    op.create_table(
        "remediation",
        _uuid_pk(),
        _uuid("tenant_id"),
        _uuid("pull_request_id"),
        _uuid("investigation_id"),
        _uuid("repository_id"),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("merge_sha", sa.String(length=64), nullable=False),
        sa.Column("rollback_target", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="OFFERED"),
        sa.Column("baseline", postgresql.JSONB(), nullable=True),
        sa.Column("policy", postgresql.JSONB(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        _uuid("started_by", nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("finished_at", nullable=True),
        sa.ForeignKeyConstraint(["pull_request_id"], [f"{SCHEMA}.pull_request.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["investigation_id"], [f"{SCHEMA}.investigation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], [f"{SCHEMA}.repository_connection.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("pull_request_id", "environment", name="uq_remediation_pull_request_environment"),
        sa.CheckConstraint(_in("status", STATUSES), name="ck_remediation_status"),
        schema=SCHEMA,
    )
    op.create_index("ix_remediation_tenant", "remediation", ["tenant_id", "created_at"], schema=SCHEMA)
    op.create_index(
        "uq_remediation_active_environment",
        "remediation",
        ["repository_id", "environment"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(_in("status", ACTIVE)),
    )

    op.create_table(
        "deployment",
        _uuid_pk(),
        _uuid("tenant_id"),
        _uuid("remediation_id"),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("sha", sa.String(length=64), nullable=False),
        _uuid("correlation_id"),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("run_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="dispatched"),
        _timestamp("dispatched_at"),
        _timestamp("finished_at", nullable=True),
        sa.ForeignKeyConstraint(["remediation_id"], [f"{SCHEMA}.remediation.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("correlation_id", name="uq_deployment_correlation"),
        sa.UniqueConstraint("remediation_id", "kind", name="uq_deployment_remediation_kind"),
        sa.CheckConstraint(_in("kind", ("deploy", "rollback")), name="ck_deployment_kind"),
        sa.CheckConstraint(_in("status", ("cancelled", "dispatched", "failed", "running", "succeeded", "timeout")), name="ck_deployment_status"),
        schema=SCHEMA,
    )

    op.create_table(
        "verification",
        _uuid_pk(),
        _uuid("tenant_id"),
        _uuid("remediation_id"),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("numbers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        _timestamp("window_start"),
        _timestamp("window_end"),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(["remediation_id"], [f"{SCHEMA}.remediation.id"], ondelete="CASCADE"),
        sa.CheckConstraint(_in("kind", ("deploy", "rollback")), name="ck_verification_kind"),
        sa.CheckConstraint(_in("verdict", ("degraded", "healthy", "inconclusive")), name="ck_verification_verdict"),
        schema=SCHEMA,
    )
    op.create_index("ix_verification_remediation", "verification", ["remediation_id", "created_at"], schema=SCHEMA)

    op.create_table(
        "approval",
        _uuid_pk(),
        _uuid("tenant_id"),
        _uuid("remediation_id"),
        sa.Column("gate", sa.String(length=12), nullable=False),
        sa.Column("decision", sa.String(length=12), nullable=False),
        _uuid("user_id"),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("sha", sa.String(length=64), nullable=False),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(["remediation_id"], [f"{SCHEMA}.remediation.id"], ondelete="CASCADE"),
        sa.CheckConstraint(_in("gate", ("decision", "deploy", "rollback")), name="ck_approval_gate"),
        schema=SCHEMA,
    )
    op.create_index("ix_approval_remediation", "approval", ["remediation_id", "created_at"], schema=SCHEMA)

    op.create_table(
        "release_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        _uuid("tenant_id"),
        _uuid("repository_id"),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("sha", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=12), nullable=False),
        _uuid("remediation_id", nullable=True),
        _timestamp("recorded_at"),
        sa.ForeignKeyConstraint(["repository_id"], [f"{SCHEMA}.repository_connection.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["remediation_id"], [f"{SCHEMA}.remediation.id"], ondelete="SET NULL"),
        sa.CheckConstraint(_in("source", ("github", "log0", "manual")), name="ck_release_ledger_source"),
        schema=SCHEMA,
    )
    op.create_index("ix_release_ledger_environment", "release_ledger", ["repository_id", "environment", "recorded_at"], schema=SCHEMA)

    op.create_table(
        "deploy_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        _uuid("tenant_id"),
        _uuid("remediation_id", nullable=True),
        _uuid("actor_id", nullable=True),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("environment", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("sha", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        _timestamp("created_at"),
        schema=SCHEMA,
    )
    op.create_index("ix_deploy_audit_tenant", "deploy_audit", ["tenant_id", "created_at"], schema=SCHEMA)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_audit_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'the deploy audit is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER deploy_audit_append_only
        BEFORE UPDATE OR DELETE ON {SCHEMA}.deploy_audit
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_audit_change()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER deploy_audit_no_truncate
        BEFORE TRUNCATE ON {SCHEMA}.deploy_audit
        FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_audit_change()
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE {SCHEMA}.deploy_audit")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_audit_change()")
    for table in ("release_ledger", "approval", "verification", "deployment", "remediation", "deploy_target"):
        op.drop_table(table, schema=SCHEMA)
