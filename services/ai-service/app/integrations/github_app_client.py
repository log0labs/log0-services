"""Read calls the GitHub App makes: who installed it, and which repositories it can reach.

Authentication is ``GitHubAppAuth``'s job; this client only asks questions. Installation lookups
are signed as the app, repository listings use a read-only installation token.
"""

import logging
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.integrations.github import (
    GitHubUnavailableError,
    api_headers,
    raise_for_status,
)
from app.integrations.github_app import GitHubAppAuth

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_PAGES = 5  # an installation is capped at 500 repositories here; anything beyond is not listed

READ_PERMISSIONS = {"metadata": "read"}


@dataclass(frozen=True)
class InstallationInfo:
    installation_id: int
    account_login: str
    account_type: str
    suspended: bool
    permissions: dict[str, str] = field(default_factory=dict)  # what the installation was granted, e.g. {"actions": "write"}


@dataclass(frozen=True)
class InstalledRepository:
    github_id: int
    owner: str
    name: str
    default_branch: str
    private: bool


class GitHubAppClient:
    def __init__(self, settings: Settings, auth: GitHubAppAuth) -> None:
        self._base = settings.github_api_url.rstrip("/")
        self._timeout = settings.github_timeout_seconds
        self.auth = auth

    async def get_installation(self, installation_id: int) -> InstallationInfo:
        """Look an installation up as the app. ``GitHubAccessError(not_found)`` if it does not exist."""
        response = await self._get(f"/app/installations/{int(installation_id)}", self.auth.app_jwt())
        try:
            body = response.json()
            return InstallationInfo(
                installation_id=int(body["id"]),
                account_login=str(body["account"]["login"]),
                account_type=str(body["account"].get("type", "User")),
                suspended=body.get("suspended_at") is not None,
                permissions={str(k): str(v) for k, v in (body.get("permissions") or {}).items()},
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise GitHubUnavailableError from None

    async def list_repositories(self, installation_id: int) -> list[InstalledRepository]:
        """Repositories the installation can reach (first ``MAX_PAGES`` pages)."""
        token = (await self.auth.installation_token(installation_id, permissions=READ_PERMISSIONS)).token.get_secret_value()
        found: list[InstalledRepository] = []
        for page in range(1, MAX_PAGES + 1):
            response = await self._get("/installation/repositories", token, {"per_page": PAGE_SIZE, "page": page})
            try:
                rows = response.json()["repositories"]
                found.extend(
                    InstalledRepository(
                        github_id=int(row["id"]),
                        owner=str(row["owner"]["login"]),
                        name=str(row["name"]),
                        default_branch=str(row.get("default_branch", "main")),
                        private=bool(row.get("private", False)),
                    )
                    for row in rows
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                raise GitHubUnavailableError from None
            if len(rows) < PAGE_SIZE:
                break
        return found

    async def _get(self, path: str, bearer: str, params: dict[str, int] | None = None) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(f"{self._base}{path}", headers=api_headers(bearer), params=params)
        except httpx.HTTPError as exc:
            logger.warning("GitHub App request failed: %s", type(exc).__name__)
            raise GitHubUnavailableError from None
        raise_for_status(response, 200)
        return response
