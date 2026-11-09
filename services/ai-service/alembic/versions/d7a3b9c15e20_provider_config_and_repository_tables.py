"""tenant LLM settings and repository connections

Revision ID: d7a3b9c15e20
Revises: c4d8e2f1a6b7
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d7a3b9c15e20"
down_revision: str | Sequence[str] | None = "c4d8e2f1a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"

PURPOSES = ("summary", "triage", "investigation", "evaluation")
REPOSITORY_PROVIDERS = ("github",)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # One row per tenant per purpose. No row means "use the platform's LLM_* settings".
    op.create_table(
        "provider_config",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        # The API key is only ever stored encrypted. key_id says which master key encrypted it
        # (so keys can be rotated); key_last4 is what the console may show ("...a1b2").
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("key_id", sa.String(length=64), nullable=True),
        sa.Column("key_last4", sa.String(length=8), nullable=True),
        sa.Column("temperature", sa.Numeric(3, 2), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "purpose", name="pk_provider_config"),
        sa.CheckConstraint(_in_list("purpose", PURPOSES), name="ck_provider_config_purpose"),
        sa.CheckConstraint(
            "(api_key_ciphertext IS NULL) = (key_id IS NULL) AND (api_key_ciphertext IS NULL) = (key_last4 IS NULL)",
            name="ck_provider_config_key_complete",
        ),
        sa.CheckConstraint("temperature IS NULL OR (temperature >= 0 AND temperature <= 2)", name="ck_provider_config_temperature"),
        sa.CheckConstraint("max_tokens IS NULL OR max_tokens > 0", name="ck_provider_config_max_tokens"),
        schema=SCHEMA,
    )

    # A repository the tenant connected, with the access token encrypted.
    op.create_table(
        "repository_connection",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="github"),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False, server_default="main"),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_in_list("provider", REPOSITORY_PROVIDERS), name="ck_repository_connection_provider"),
        sa.UniqueConstraint("tenant_id", "provider", "owner", "name", name="uq_repository_connection_repo"),
        # Not needed to identify a row (id already does). It exists so service_repository can
        # point at (tenant_id, id) and the database itself refuses a cross-tenant link.
        sa.UniqueConstraint("tenant_id", "id", name="uq_repository_connection_tenant_id"),
        schema=SCHEMA,
    )

    # Which repository (and which folder of it) holds the code of a service.
    op.create_table(
        "service_repository",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_name", sa.String(length=255), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path_prefix", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("tenant_id", "service_name", name="pk_service_repository"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "repository_id"],
            [f"{SCHEMA}.repository_connection.tenant_id", f"{SCHEMA}.repository_connection.id"],
            name="fk_service_repository_repository",
            ondelete="CASCADE",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_service_repository_repository", "service_repository", ["repository_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_service_repository_repository", table_name="service_repository", schema=SCHEMA)
    op.drop_table("service_repository", schema=SCHEMA)
    op.drop_table("repository_connection", schema=SCHEMA)
    op.drop_table("provider_config", schema=SCHEMA)
