"""``/api/v1/repositories`` and ``/api/v1/service-repositories``: which code belongs to which service.

Anyone signed in can read; only an ADMIN can connect or disconnect a repository or change a
mapping. The GitHub token is checked against GitHub, encrypted, stored, and never returned.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_github, require_secret_box
from app.db.session import get_db
from app.integrations.github import (
    GitHubAccessError,
    GitHubClient,
    GitHubUnavailableError,
)
from app.schemas.ai_config import (
    RepositoryIn,
    RepositoryOut,
    ServiceRepositoryIn,
    ServiceRepositoryOut,
)
from app.security.crypto import SecretBox, secret_context
from app.security.jwt import CurrentUser, get_current_user, require_admin
from app.store import repositories as store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/repositories", tags=["repositories"])
services_router = APIRouter(prefix="/api/v1/service-repositories", tags=["repositories"])

_GITHUB_PROBLEMS = {
    "bad_token": "GitHub rejected the token",
    "forbidden": "The token is not allowed to read this repository (or GitHub is rate limiting it)",
    "not_found": "Repository not found, or the token cannot see it",
}


def _out(row: store.RepositoryConnection) -> RepositoryOut:
    return RepositoryOut(
        id=row.id,
        provider=row.provider,
        owner=row.owner,
        name=row.name,
        default_branch=row.default_branch,
        created_at=row.created_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RepositoryOut)
async def connect_repository(
    body: RepositoryIn,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    github: Annotated[GitHubClient, Depends(get_github)],
    box: Annotated[SecretBox, Depends(require_secret_box)],
) -> RepositoryOut:
    token = body.token.get_secret_value()
    try:
        found = await github.get_repository(body.owner, body.name, token)
    except GitHubAccessError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_GITHUB_PROBLEMS[exc.problem]) from None
    except GitHubUnavailableError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub could not be reached") from None

    # GitHub's own spelling of owner and name, so "ACME/Shop" and "acme/shop" are one repository.
    encrypted = box.encrypt(token, context=secret_context(admin.tenant_id, "repository_connection", f"{found.owner}/{found.name}"))
    try:
        row = await store.create(
            session,
            tenant_id=admin.tenant_id,
            owner=found.owner,
            name=found.name,
            default_branch=found.default_branch,
            token=encrypted,
            created_by=admin.user_id,
        )
    except store.DuplicateRepositoryError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This repository is already connected") from None
    logger.info("repository connected: tenant=%s repository=%s", admin.tenant_id, row.id)
    return _out(row)


@router.get("", response_model=list[RepositoryOut])
async def list_repositories(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[RepositoryOut]:
    return [_out(row) for row in await store.list_for_tenant(session, tenant_id=user.tenant_id)]


@router.delete("/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_repository(
    repository_id: UUID,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if not await store.delete_one(session, tenant_id=admin.tenant_id, repository_id=repository_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ service mappings

ServiceName = Annotated[str, Path(min_length=1, max_length=255)]


@services_router.get("", response_model=list[ServiceRepositoryOut])
async def list_service_repositories(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[ServiceRepositoryOut]:
    rows = await store.list_services(session, tenant_id=user.tenant_id)
    return [ServiceRepositoryOut(service_name=r.service_name, repository_id=r.repository_id, path_prefix=r.path_prefix) for r in rows]


@services_router.put("/{service_name}", response_model=ServiceRepositoryOut)
async def map_service(
    service_name: ServiceName,
    body: ServiceRepositoryIn,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ServiceRepositoryOut:
    try:
        row = await store.upsert_service(
            session,
            tenant_id=admin.tenant_id,
            service_name=service_name,
            repository_id=body.repository_id,
            path_prefix=body.path_prefix,
        )
    except store.RepositoryNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found") from None
    return ServiceRepositoryOut(service_name=row.service_name, repository_id=row.repository_id, path_prefix=row.path_prefix)


@services_router.delete("/{service_name}", status_code=status.HTTP_204_NO_CONTENT)
async def unmap_service(
    service_name: ServiceName,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    if not await store.delete_service(session, tenant_id=admin.tenant_id, service_name=service_name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
