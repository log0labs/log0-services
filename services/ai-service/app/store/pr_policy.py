"""Persistence for ai.pr_policy: a workspace's own pull request rules (one row per tenant)."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import pr_policy
from app.publishing.policy import TenantRules

_FIELDS = ("only_on_green", "max_files", "max_changed_lines", "max_per_repository_per_hour", "max_per_tenant_per_hour")


async def get(session: AsyncSession, *, tenant_id: UUID) -> TenantRules | None:
    row = (await session.execute(select(pr_policy).where(pr_policy.c.tenant_id == tenant_id))).first()
    return TenantRules(**{name: row._mapping[name] for name in _FIELDS}) if row else None


async def upsert(session: AsyncSession, *, tenant_id: UUID, rules: TenantRules, updated_by: UUID) -> TenantRules:
    values = {name: getattr(rules, name) for name in _FIELDS}
    await session.execute(
        insert(pr_policy)
        .values(tenant_id=tenant_id, updated_by=updated_by, **values)
        .on_conflict_do_update(index_elements=[pr_policy.c.tenant_id], set_={**values, "updated_by": updated_by})
    )
    await session.commit()
    return rules


async def delete_for(session: AsyncSession, *, tenant_id: UUID) -> bool:
    result = await session.execute(delete(pr_policy).where(pr_policy.c.tenant_id == tenant_id))
    await session.commit()
    return result.rowcount > 0
