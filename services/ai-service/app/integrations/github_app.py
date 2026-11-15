"""Authentication as the log0 GitHub App: app JWT, then short-lived installation tokens.

V2 writes to customer repositories only through an installation token. A token is minted per
use, narrowed to the repositories and permissions the caller names, held in memory until shortly
before it expires, and never logged, stored or put in an error message.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import jwt
from pydantic import SecretStr

from app.config import Settings
from app.integrations.github import (
    GitHubAccessError,
    GitHubUnavailableError,
    api_headers,
)

logger = logging.getLogger(__name__)

APP_JWT_LIFETIME_SECONDS = 540  # GitHub allows at most 10 minutes
APP_JWT_BACKDATE_SECONDS = 60  # tolerate clock drift between us and GitHub
TOKEN_REFRESH_MARGIN_SECONDS = 120  # a cached token is reused only while it has this long left

WRITE_PERMISSIONS: Mapping[str, str] = {"contents": "write", "pull_requests": "write", "checks": "read", "metadata": "read"}
"""The least the repair flow needs. Workflows, administration and secrets are never requested."""


class GitHubAppNotConfiguredError(Exception):
    """GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is missing, or the key is not a usable RSA key."""


@dataclass(frozen=True)
class InstallationToken:
    token: SecretStr
    expires_at: datetime
    repository_ids: tuple[int, ...] = field(default=())

    def seconds_left(self, now: datetime) -> float:
        return (self.expires_at - now).total_seconds()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GitHubAppAuth:
    """Signs app JWTs and exchanges them for installation tokens, with a small in-memory cache."""

    def __init__(self, settings: Settings, clock: Callable[[], datetime] = _utcnow) -> None:
        self._app_id = settings.github_app_id
        self._private_key = settings.github_app_private_key
        self._base = settings.github_api_url.rstrip("/")
        self._timeout = settings.github_timeout_seconds
        self._clock = clock
        self._cache: dict[tuple[int, tuple[int, ...], tuple[tuple[str, str], ...]], InstallationToken] = {}

    @property
    def configured(self) -> bool:
        return bool(self._app_id) and self._private_key is not None

    def app_jwt(self) -> str:
        """A JWT that proves we are the app (RS256, valid a few minutes)."""
        if not self.configured or self._private_key is None:
            raise GitHubAppNotConfiguredError("GitHub App is not configured")
        issued = int(self._clock().timestamp())
        claims = {"iss": self._app_id, "iat": issued - APP_JWT_BACKDATE_SECONDS, "exp": issued + APP_JWT_LIFETIME_SECONDS}
        pem = self._private_key.get_secret_value().replace("\\n", "\n")
        try:
            return jwt.encode(claims, pem, algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError):
            raise GitHubAppNotConfiguredError("GitHub App private key is not a valid RSA key") from None

    async def installation_token(
        self,
        installation_id: int,
        repository_ids: Sequence[int] | None = None,
        permissions: Mapping[str, str] = WRITE_PERMISSIONS,
    ) -> InstallationToken:
        """A token for one installation, narrowed to ``repository_ids`` and ``permissions``.

        Raises ``GitHubAccessError`` (``not_found``: the installation was removed, ``forbidden``:
        suspended or permissions not granted, ``bad_token``: GitHub rejected our app JWT) or
        ``GitHubUnavailableError`` (network, 5xx, unreadable answer).
        """
        repositories = tuple(sorted(set(repository_ids or ())))
        key = (installation_id, repositories, tuple(sorted(permissions.items())))
        cached = self._cache.get(key)
        if cached is not None and cached.seconds_left(self._clock()) > TOKEN_REFRESH_MARGIN_SECONDS:
            return cached

        body: dict[str, object] = {"permissions": dict(permissions)}
        if repositories:
            body["repository_ids"] = list(repositories)
        response = await self._post(f"/app/installations/{int(installation_id)}/access_tokens", body)
        token = self._parse(response, repositories)
        self._cache[key] = token
        return token

    def forget(self, installation_id: int) -> None:
        """Drop cached tokens of an installation (it was suspended or removed)."""
        for key in [k for k in self._cache if k[0] == installation_id]:
            del self._cache[key]

    async def _post(self, path: str, body: dict[str, object]) -> httpx.Response:
        headers = api_headers(self.app_jwt())
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(f"{self._base}{path}", headers=headers, json=body)
        except httpx.HTTPError as exc:
            logger.warning("GitHub App request failed: %s", type(exc).__name__)
            raise GitHubUnavailableError from None
        if response.status_code == 401:
            raise GitHubAccessError("bad_token")
        if response.status_code in (403, 422):  # suspended, or asked for access the app does not have
            raise GitHubAccessError("forbidden")
        if response.status_code == 404:
            raise GitHubAccessError("not_found")
        if response.status_code != 201:
            logger.warning("GitHub answered %s to an installation token request", response.status_code)
            raise GitHubUnavailableError
        return response

    @staticmethod
    def _parse(response: httpx.Response, repositories: tuple[int, ...]) -> InstallationToken:
        try:
            data = response.json()
            expires_at = datetime.fromisoformat(str(data["expires_at"]))
            return InstallationToken(SecretStr(str(data["token"])), expires_at, repositories)
        except (KeyError, TypeError, ValueError):
            raise GitHubUnavailableError from None

