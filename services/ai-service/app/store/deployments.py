"""Persistence for ai.deployment: the pipeline run log0 asked for (one deploy and at most one rollback per remediation).

``ensure`` is the idempotency of the whole deploy: the correlation id is generated once, the first time a remediation
asks to deploy, and every later attempt (a resume after a crash) gets the same row back, so the same id is looked for in
the pipeline instead of a second dispatch being made blindly. Every function filters by ``tenant_id``.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import deployment
from app.schemas.remediation import DeployKind, DeploymentStatus


@dataclass(frozen=True)
class Deployment:
    id: UUID
    tenant_id: UUID
    remediation_id: UUID
    kind: str
    environment: str
    sha: str
    correlation_id: UUID
    run_id: int | None
    run_url: str | None
    status: str
    dispatched_at: datetime
    finished_at: datetime | None

    @property
    def finished(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled", "timeout")


def _to_deployment(row) -> Deployment:
    m = row._mapping
    return Deployment(**{name: m[name] for name in Deployment.__dataclass_fields__})


async def ensure(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    remediation_id: UUID,
    kind: DeployKind,
    environment: str,
    sha: str,
    now: datetime,
) -> tuple[Deployment, bool]:
    """The deployment of this remediation and kind, and whether this call created it. ``now`` is the run's clock: every deadline
    of a deploy (the grace for the run to appear, the timeout) is measured on it, not on the database's."""
    created = (
        await session.execute(
            insert(deployment)
            .values(tenant_id=tenant_id, remediation_id=remediation_id, kind=kind, environment=environment, sha=sha, correlation_id=uuid4(), dispatched_at=now)
            .on_conflict_do_nothing(index_elements=[deployment.c.remediation_id, deployment.c.kind])
            .returning(deployment)
        )
    ).first()
    await session.commit()
    if created is not None:
        return _to_deployment(created), True
    existing = await get(session, tenant_id=tenant_id, remediation_id=remediation_id, kind=kind)
    assert existing is not None  # the conflicting row is this tenant's own: the remediation id is
    return existing, False


async def get(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID, kind: DeployKind) -> Deployment | None:
    row = (
        await session.execute(select(deployment).where(deployment.c.tenant_id == tenant_id, deployment.c.remediation_id == remediation_id, deployment.c.kind == kind))
    ).first()
    return _to_deployment(row) if row else None


async def update_run(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    deployment_id: UUID,
    status: DeploymentStatus,
    run_id: int | None = None,
    run_url: str | None = None,
) -> None:
    """Record what the pipeline run is doing. A finished status also stamps ``finished_at``; ``run_id`` and ``run_url`` are only ever filled in."""
    values: dict[str, object] = {"status": status}
    if run_id is not None:
        values["run_id"] = run_id
    if run_url:
        values["run_url"] = run_url
    if status in ("succeeded", "failed", "cancelled", "timeout"):
        values["finished_at"] = func.now()
    await session.execute(update(deployment).where(deployment.c.tenant_id == tenant_id, deployment.c.id == deployment_id).values(**values))
    await session.commit()
