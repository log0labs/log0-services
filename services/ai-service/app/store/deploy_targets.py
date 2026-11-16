"""Persistence for ai.deploy_target: how a tenant deploys one repository (one row per repository).

Every function filters by ``tenant_id``. The configuration is validated by ``DeployTargetConfig`` on the way in and on
the way out, so a row that was edited by hand and no longer fits the shape is reported instead of trusted.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import deploy_target
from app.deploy.target import DeployTargetConfig


@dataclass(frozen=True)
class DeployTarget:
    id: UUID
    tenant_id: UUID
    repository_id: UUID
    config: DeployTargetConfig
    updated_by: UUID
    updated_at: datetime


def _to_target(row) -> DeployTarget:
    m = row._mapping
    config = DeployTargetConfig.model_validate({"workflow_file": m["workflow_file"], "environments": m["environments"]})
    return DeployTarget(m["id"], m["tenant_id"], m["repository_id"], config, m["updated_by"], m["updated_at"])


async def get(session: AsyncSession, *, tenant_id: UUID, repository_id: UUID) -> DeployTarget | None:
    row = (await session.execute(select(deploy_target).where(deploy_target.c.tenant_id == tenant_id, deploy_target.c.repository_id == repository_id))).first()
    return _to_target(row) if row else None


async def upsert(session: AsyncSession, *, tenant_id: UUID, repository_id: UUID, config: DeployTargetConfig, updated_by: UUID) -> DeployTarget:
    """Create or replace the target of a repository. The caller has already checked the repository is the tenant's."""
    values = {"workflow_file": config.workflow_file, "environments": config.model_dump(mode="json")["environments"], "updated_by": updated_by}
    row = (
        await session.execute(
            insert(deploy_target)
            .values(tenant_id=tenant_id, repository_id=repository_id, **values)
            .on_conflict_do_update(index_elements=[deploy_target.c.repository_id], set_=values | {"updated_at": func.now()}, where=deploy_target.c.tenant_id == tenant_id)
            .returning(deploy_target)
        )
    ).first()
    await session.commit()
    assert row is not None  # the repository belongs to this tenant (checked by the caller), so the conflict update applied
    return _to_target(row)


async def delete_for(session: AsyncSession, *, tenant_id: UUID, repository_id: UUID) -> bool:
    result = await session.execute(delete(deploy_target).where(deploy_target.c.tenant_id == tenant_id, deploy_target.c.repository_id == repository_id))
    await session.commit()
    return result.rowcount > 0
