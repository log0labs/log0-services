"""The little of GitHub's API that connecting a repository needs: can this token read it?

Step 3.4 clones with the same token; this client only checks access and learns the canonical
owner, name and default branch. The token is sent in the ``Authorization`` header and nowhere
else: never in a URL, a log line or an error message.
"""

import logging
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

AccessProblem = Literal["bad_token", "not_found", "forbidden"]


class GitHubAccessError(Exception):
    """GitHub answered, and the answer is "no": the caller can fix it (token, name, permissions)."""

    def __init__(self, problem: AccessProblem) -> None:
        super().__init__(problem)
        self.problem = problem


class GitHubUnavailableError(Exception):
    """GitHub could not be reached or failed (network error, 5xx, unreadable answer)."""


@dataclass(frozen=True)
class GitHubRepository:
    owner: str  # as GitHub spells it, so the same repository is never stored twice in two cases
    name: str
    default_branch: str
    private: bool


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self._base = settings.github_api_url.rstrip("/")
        self._timeout = settings.github_timeout_seconds

    async def get_repository(self, owner: str, name: str, token: str) -> GitHubRepository:
        """Fetch a repository with the tenant's token. ``owner`` and ``name`` must be validated."""
        url = f"{self._base}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "log0-ai-service",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("GitHub request failed: %s", type(exc).__name__)  # class only: messages can echo URLs
            raise GitHubUnavailableError from None

        if response.status_code == 401:
            raise GitHubAccessError("bad_token")
        if response.status_code in (403, 429):  # no permission, or rate limited
            raise GitHubAccessError("forbidden")
        if response.status_code == 404:  # GitHub answers 404 for a private repository the token cannot see
            raise GitHubAccessError("not_found")
        if response.status_code != 200:
            logger.warning("GitHub answered %s for a repository lookup", response.status_code)
            raise GitHubUnavailableError

        try:
            body = response.json()
            permissions = body.get("permissions") or {}
            repository = GitHubRepository(
                owner=str(body["owner"]["login"]),
                name=str(body["name"]),
                default_branch=str(body["default_branch"]),
                private=bool(body.get("private", False)),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise GitHubUnavailableError from None
        if permissions.get("pull") is False:
            raise GitHubAccessError("forbidden")
        return repository
