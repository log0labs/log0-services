"""``POST /api/v1/github/webhook``: GitHub tells us when the app is removed, suspended or loses repositories, and when a pull request log0 opened is merged or closed.

There is no user here: the request is trusted only if its HMAC signature matches
``GITHUB_WEBHOOK_SECRET``. Handling is idempotent (GitHub redelivers), ignores events it does not
know, and never trusts the payload for anything but which installation or repositories changed.
"""

import json
import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_github_app
from app.config import Settings, get_settings
from app.db.session import get_db
from app.integrations.github_app_client import GitHubAppClient
from app.security.webhook_signature import verify_github_signature
from app.store import deploy_targets, pull_requests, remediations
from app.store import events as event_store
from app.store import github_installations as installations
from app.store import repositories as repository_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["github"])

MAX_BODY_BYTES = 1_000_000
_INSTALLATION_EVENTS = {"installation", "installation_repositories", "pull_request"}


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[GitHubAppClient, Depends(get_github_app)],
    settings: Annotated[Settings, Depends(get_settings)],
    event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> dict[str, bool]:
    if settings.github_webhook_secret is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhooks are not configured")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Payload too large")
    if not verify_github_signature(settings.github_webhook_secret.get_secret_value(), body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
    try:
        payload = json.loads(body)
        installation_id = int(payload["installation"]["id"]) if event in _INSTALLATION_EVENTS else None
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unreadable payload") from None
    handled = await _handle(event, payload, installation_id, session, client)
    return {"handled": handled}



async def _handle(event: str | None, payload: dict[str, Any], installation_id: int | None, session: AsyncSession, client: GitHubAppClient) -> bool:
    if installation_id is None:
        return False
    action = payload.get("action")
    if event == "pull_request":
        return await _pull_request_changed(payload, installation_id, action, session)
    if event == "installation":
        if action == "deleted":
            await installations.delete_by_installation(session, installation_id=installation_id)
        elif action in ("suspend", "unsuspend"):
            await installations.set_suspended_by_installation(session, installation_id=installation_id, suspended=action == "suspend")
        else:
            return False
        client.auth.forget(installation_id)
        logger.info("github installation %s: %s", installation_id, action)
        return True
    removed = [int(r["id"]) for r in payload.get("repositories_removed") or [] if isinstance(r, dict) and "id" in r]
    if action == "removed" and removed:
        await repository_store.disable_writes_for_github_repositories(session, installation_id=installation_id, github_repository_ids=removed)
    client.auth.forget(installation_id)  # cached tokens were narrowed to the old repository list
    return True


_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_PULL_REQUEST_STATES = {"closed": None, "reopened": "open"}  # closed is merged or closed, decided by the payload


async def _pull_request_changed(payload: dict[str, Any], installation_id: int, action: Any, session: AsyncSession) -> bool:
    """A human merged, closed or reopened a pull request: keep the record, and say so in the investigation's log."""
    if action not in _PULL_REQUEST_STATES:
        return False
    try:
        number = int(payload["pull_request"]["number"])
        repository_id = int(payload["repository"]["id"])
        merged = bool(payload["pull_request"].get("merged"))
    except (KeyError, TypeError, ValueError):
        return False
    state = _PULL_REQUEST_STATES[action] or ("merged" if merged else "closed")
    merge_sha = str(payload["pull_request"].get("merge_commit_sha") or "")
    row = await pull_requests.sync_state(
        session,
        installation_id=installation_id,
        github_repository_id=repository_id,
        number=number,
        state=state,
        merge_commit_sha=merge_sha if _COMMIT_SHA.fullmatch(merge_sha) else None,
    )
    if row is None:
        return False
    await event_store.append_event(session, investigation_id=row.investigation_id, type=f"pr.{state}", payload={"number": number})
    logger.info("pull request %s of investigation %s is now %s", number, row.investigation_id, state)
    if state == "merged" and row.merge_commit_sha:
        await _offer_deploys(session, row)
    return True


async def _offer_deploys(session: AsyncSession, row: pull_requests.PullRequestRow) -> None:
    """A log0 pull request was merged: offer a deploy to each enabled environment of the repository, if it has a target.

    Only an offer. Nothing is deployed until a human starts one and then approves it (ADR-019).
    """
    try:
        target = await deploy_targets.get(session, tenant_id=row.tenant_id, repository_id=row.repository_id)
    except ValidationError:
        logger.warning("the deploy target of repository %s no longer fits its shape: no offer made", row.repository_id)
        return
    if target is None or row.merge_commit_sha is None:
        return
    enabled = [environment.name for environment in target.config.environments if environment.enabled]
    created = await remediations.offer_for_merge(
        session,
        tenant_id=row.tenant_id,
        pull_request_id=row.id,
        investigation_id=row.investigation_id,
        repository_id=row.repository_id,
        merge_sha=row.merge_commit_sha,
        environments=enabled,
    )
    if created:
        offered = [r.environment for r in created]
        await event_store.append_event(session, investigation_id=row.investigation_id, type="remediation.offered", payload={"environments": offered})
        logger.info("deploy offered for pull request %s to %s", row.number, offered)
