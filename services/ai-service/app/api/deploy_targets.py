"""``/api/v1/deploy-targets``: how a repository is deployed (the workflow to ask for, the rules of each environment).

ADMIN only. Only a repository of the caller's own tenant can have a target; any other id looks like a missing one.
Nothing here deploys anything: it stores the strictly shaped configuration the remediation graph will read.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deploy.target import DeployTargetConfig, rules
from app.schemas.deploy_target import DeployTargetOut
from app.security.jwt import CurrentUser, require_admin
from app.store import deploy_targets as targets
from app.store import repositories as repository_store

router = APIRouter(prefix="/api/v1/deploy-targets", tags=["deploy"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _out(row: targets.DeployTarget) -> DeployTargetOut:
    return DeployTargetOut(repository_id=row.repository_id, config=row.config, updated_at=row.updated_at)


async def _own_repository(session: AsyncSession, admin: CurrentUser, repository_id: UUID) -> None:
    if await repository_store.get(session, tenant_id=admin.tenant_id, repository_id=repository_id) is None:
        raise _NOT_FOUND


@router.get("/rules")
async def deploy_rules(_: Annotated[CurrentUser, Depends(require_admin)]) -> dict[str, object]:
    """The limits a configuration must respect, so an editor can check before it sends (the server still validates every save)."""
    return rules()


@router.get("/{repository_id}", response_model=DeployTargetOut)
async def get_target(
    repository_id: UUID,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> DeployTargetOut:
    row = await targets.get(session, tenant_id=admin.tenant_id, repository_id=repository_id)
    if row is None:
        raise _NOT_FOUND
    return _out(row)


@router.put("/{repository_id}", response_model=DeployTargetOut)
async def put_target(
    repository_id: UUID,
    body: DeployTargetConfig,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> DeployTargetOut:
    await _own_repository(session, admin, repository_id)
    row = await targets.upsert(session, tenant_id=admin.tenant_id, repository_id=repository_id, config=body, updated_by=admin.user_id)
    return _out(row)


@router.delete("/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    repository_id: UUID,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if not await targets.delete_for(session, tenant_id=admin.tenant_id, repository_id=repository_id):
        raise _NOT_FOUND
    return Response(status_code=status.HTTP_204_NO_CONTENT)
