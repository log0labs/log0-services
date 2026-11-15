"""``/api/v1/github``: install the log0 GitHub App and choose which repositories it may write to.

ADMIN only. An installation is linked through a signed ``state`` (see ``security.install_state``),
and a repository can only be switched to writing when it is one the tenant connected itself
(Settings, Repositories) and the installation can reach, so claiming somebody else's installation
id gives no access to their code.
"""

import logging
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_github_app
from app.config import Settings, get_settings
from app.db.session import get_db
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import GitHubAppNotConfiguredError
from app.integrations.github_app_client import GitHubAppClient, InstalledRepository
from app.publishing.policy import PrLimits, TenantRules, server_limits
from app.schemas.ai_config import RepositoryOut
from app.schemas.github_app import (
    AuditEntryOut,
    GitHubAppOut,
    InstallationIn,
    InstallationOut,
    InstalledRepositoryOut,
    PrPolicyIn,
    PrPolicyLimits,
    PrPolicyOut,
    WritesIn,
)
from app.security.install_state import (
    InvalidInstallStateError,
    issue_state,
    verify_state,
)
from app.security.jwt import CurrentUser, require_admin
from app.store import github_installations as installations
from app.store import pr_policy as policy_store
from app.store import repositories as repository_store
from app.store import write_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["github"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _installation_out(row: installations.Installation) -> InstallationOut:
    return InstallationOut(
        installation_id=row.installation_id,
        account_login=row.account_login,
        account_type=row.account_type,
        suspended=row.suspended,
        created_at=row.created_at,
    )


def _github_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GitHubAppNotConfiguredError):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The GitHub App is not configured on this server")
    if isinstance(exc, GitHubAccessError):
        if exc.problem == "not_found":
            return _NOT_FOUND
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="GitHub refused the request for this installation")
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub could not be reached")


def _same_repository(candidate: InstalledRepository, owner: str, name: str) -> bool:
    return (candidate.owner.lower(), candidate.name.lower()) == (owner.lower(), name.lower())


@router.get("/app", response_model=GitHubAppOut)
async def app_status(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    client: Annotated[GitHubAppClient, Depends(get_github_app)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GitHubAppOut:
    install_url = None
    if client.auth.configured and settings.github_app_slug and settings.jwt_secret is not None:
        state = issue_state(settings.jwt_secret.get_secret_value(), tenant_id=admin.tenant_id, user_id=admin.user_id)
        install_url = f"https://github.com/apps/{quote(settings.github_app_slug, safe='')}/installations/new?state={quote(state, safe='')}"
    return GitHubAppOut(configured=client.auth.configured, writes_enabled=settings.pr_writes_enabled, install_url=install_url)


@router.post("/installations", status_code=status.HTTP_201_CREATED, response_model=InstallationOut)
async def link_installation(
    body: InstallationIn,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[GitHubAppClient, Depends(get_github_app)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> InstallationOut:
    if settings.jwt_secret is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Signing is not configured on this server")
    try:
        verify_state(settings.jwt_secret.get_secret_value(), body.state, tenant_id=admin.tenant_id, user_id=admin.user_id)
    except InvalidInstallStateError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The installation link expired: start again") from None
    try:
        info = await client.get_installation(body.installation_id)
    except (GitHubAppNotConfiguredError, GitHubAccessError, GitHubUnavailableError) as exc:
        raise _github_error(exc) from None
    try:
        row = await installations.link(
            session,
            tenant_id=admin.tenant_id,
            installation_id=info.installation_id,
            account_login=info.account_login,
            account_type=info.account_type,
            suspended=info.suspended,
            created_by=admin.user_id,
        )
    except installations.InstallationTakenError:
        raise _NOT_FOUND from None
    logger.info("github installation linked: tenant=%s installation=%s", admin.tenant_id, row.installation_id)
    return _installation_out(row)


@router.get("/installations", response_model=list[InstallationOut])
async def list_installations(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[InstallationOut]:
    return [_installation_out(row) for row in await installations.list_for_tenant(session, tenant_id=admin.tenant_id)]


@router.delete("/installations/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_installation(
    installation_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[GitHubAppClient, Depends(get_github_app)],
) -> Response:
    if not await installations.delete_one(session, tenant_id=admin.tenant_id, installation_id=installation_id):
        raise _NOT_FOUND
    client.auth.forget(installation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/installations/{installation_id}/repositories", response_model=list[InstalledRepositoryOut])
async def installation_repositories(
    installation_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[GitHubAppClient, Depends(get_github_app)],
) -> list[InstalledRepositoryOut]:
    if await installations.get(session, tenant_id=admin.tenant_id, installation_id=installation_id) is None:
        raise _NOT_FOUND
    try:
        visible = await client.list_repositories(installation_id)
    except (GitHubAppNotConfiguredError, GitHubAccessError, GitHubUnavailableError) as exc:
        raise _github_error(exc) from None
    connected = await repository_store.list_for_tenant(session, tenant_id=admin.tenant_id)
    result = []
    for repo in visible:
        match = next((c for c in connected if _same_repository(repo, c.owner, c.name)), None)
        result.append(
            InstalledRepositoryOut(
                github_id=repo.github_id,
                owner=repo.owner,
                name=repo.name,
                default_branch=repo.default_branch,
                private=repo.private,
                repository_id=match.id if match else None,
                writes_enabled=bool(match and match.pr_writes_enabled and match.installation_id == installation_id),
            )
        )
    return result


@router.put("/repositories/{repository_id}/writes", response_model=RepositoryOut)
async def set_repository_writes(
    repository_id: UUID,
    body: WritesIn,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[GitHubAppClient, Depends(get_github_app)],
) -> RepositoryOut:
    repository = await repository_store.get(session, tenant_id=admin.tenant_id, repository_id=repository_id)
    if repository is None:
        raise _NOT_FOUND
    link: tuple[int, int] | None = None
    if body.enabled:
        link = await _find_writable_installation(session, client, admin.tenant_id, repository)
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="No installation of the log0 GitHub App on this workspace can reach this repository",
            )
    row = await repository_store.set_writes(
        session,
        tenant_id=admin.tenant_id,
        repository_id=repository_id,
        enabled=body.enabled,
        installation_id=link[0] if link else None,
        github_repository_id=link[1] if link else None,
    )
    logger.info("pr writes %s: tenant=%s repository=%s", "enabled" if body.enabled else "disabled", admin.tenant_id, row.id)
    return RepositoryOut(
        id=row.id,
        provider=row.provider,
        owner=row.owner,
        name=row.name,
        default_branch=row.default_branch,
        created_at=row.created_at,
        pr_writes_enabled=row.pr_writes_enabled,
        installation_id=row.installation_id,
    )


async def _find_writable_installation(
    session: AsyncSession,
    client: GitHubAppClient,
    tenant_id: UUID,
    repository: repository_store.RepositoryConnection,
) -> tuple[int, int] | None:
    """(installation id, GitHub repository id) of the first of the tenant's active installations that reaches the repository."""
    for installation in await installations.list_for_tenant(session, tenant_id=tenant_id):
        if installation.suspended:
            continue
        try:
            visible = await client.list_repositories(installation.installation_id)
        except (GitHubAppNotConfiguredError, GitHubAccessError, GitHubUnavailableError) as exc:
            raise _github_error(exc) from None
        for candidate in visible:
            if _same_repository(candidate, repository.owner, repository.name):
                return installation.installation_id, candidate.github_id
    return None


@router.get("/audit", response_model=list[AuditEntryOut])
async def audit_log(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEntryOut]:
    """Every write log0 made to this workspace's repositories, newest first."""
    rows = await write_audit.list_for_tenant(session, tenant_id=admin.tenant_id, limit=limit)
    return [
        AuditEntryOut(
            id=row.id,
            investigation_id=row.investigation_id,
            actor_id=row.actor_id,
            action=row.action,
            repository=row.repository,
            target=row.target,
            result=row.result,
            detail=row.detail,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _policy_out(rules: TenantRules | None, settings: Settings) -> PrPolicyOut:
    rules = rules or TenantRules()
    ceilings = server_limits(settings)
    return PrPolicyOut(
        only_on_green=rules.only_on_green,
        sandbox_tests=rules.sandbox_tests,
        max_files=rules.max_files,
        max_changed_lines=rules.max_changed_lines,
        max_per_repository_per_hour=rules.max_per_repository_per_hour,
        max_per_tenant_per_hour=rules.max_per_tenant_per_hour,
        server=PrPolicyLimits(
            max_files=ceilings.max_files,
            max_changed_lines=ceilings.max_changed_lines,
            max_per_repository_per_hour=ceilings.max_per_repository_per_hour,
            max_per_tenant_per_hour=ceilings.max_per_tenant_per_hour,
        ),
        server_only_on_green=settings.pr_only_on_green,
        server_sandbox_available=settings.sandbox_enabled,
    )


@router.get("/policy", response_model=PrPolicyOut)
async def get_policy(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PrPolicyOut:
    return _policy_out(await policy_store.get(session, tenant_id=admin.tenant_id), settings)


@router.put("/policy", response_model=PrPolicyOut)
async def put_policy(
    body: PrPolicyIn,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PrPolicyOut:
    """Make the pull request rules of this workspace stricter than the server's. Looser values are refused."""
    ceilings: PrLimits = server_limits(settings)
    for name in ("max_files", "max_changed_lines", "max_per_repository_per_hour", "max_per_tenant_per_hour"):
        chosen, ceiling = getattr(body, name), getattr(ceilings, name)
        if chosen is not None and chosen > ceiling:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{name} cannot be above the server limit of {ceiling}")
    if body.sandbox_tests and not settings.sandbox_enabled:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The test sandbox is not available on this server")
    rules = TenantRules(**{name: getattr(body, name) for name in TenantRules.__dataclass_fields__})
    saved = await policy_store.upsert(session, tenant_id=admin.tenant_id, rules=rules, updated_by=admin.user_id)
    return _policy_out(saved, settings)


@router.delete("/policy", status_code=status.HTTP_204_NO_CONTENT)
async def reset_policy(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    await policy_store.delete_for(session, tenant_id=admin.tenant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
