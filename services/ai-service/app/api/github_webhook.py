"""``POST /api/v1/github/webhook``: GitHub tells us when the app is removed, suspended or loses repositories.

There is no user here: the request is trusted only if its HMAC signature matches
``GITHUB_WEBHOOK_SECRET``. Handling is idempotent (GitHub redelivers), ignores events it does not
know, and never trusts the payload for anything but which installation or repositories changed.
"""

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_github_app
from app.config import Settings, get_settings
from app.db.session import get_db
from app.integrations.github_app_client import GitHubAppClient
from app.security.webhook_signature import verify_github_signature
from app.store import github_installations as installations
from app.store import repositories as repository_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["github"])

MAX_BODY_BYTES = 1_000_000
_INSTALLATION_EVENTS = {"installation", "installation_repositories"}


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
