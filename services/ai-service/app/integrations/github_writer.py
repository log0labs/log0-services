"""The only code that writes to a customer repository: branch, commit, draft pull request.

Everything goes through GitHub's Git Data API with a one-repository installation token, so no write
credential is ever on disk and no clone is pushed. The calls are built to be repeated safely: asking
for a branch, a commit or a pull request that already exists returns it instead of failing, which is
what lets a resumed run continue where it stopped.

Two rules are enforced here and not left to callers: a branch must live under ``log0/`` (the
default branch and anyone else's branches are never touched), and a branch that already exists is
reused only when it is ours (at the base commit, or at a commit carrying our investigation marker).
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.integrations.github import (
    GitHubAccessError,
    GitHubUnavailableError,
    api_headers,
    raise_for_status,
)
from app.integrations.github_app import WRITE_PERMISSIONS, GitHubAppAuth
from app.schemas.domain import CheckResult

logger = logging.getLogger(__name__)

BRANCH_PREFIX = "log0/"
MARKER_PREFIX = "log0-investigation:"
LABELS = ("log0",)


class BranchRefusedError(Exception):
    """The branch name is not one log0 may write to, or the branch exists and is not ours."""


class WriteRejectedError(Exception):
    """GitHub understood the request and said no (conflict, validation); the message is safe to show."""


@dataclass(frozen=True)
class WriteTarget:
    """Where to write: one repository of one installation."""

    owner: str
    name: str
    installation_id: int
    github_repository_id: int
    default_branch: str


@dataclass(frozen=True)
class FileWrite:
    """One file of the commit. ``content`` is the whole new text, or ``None`` to delete the file."""

    path: str
    content: str | None
    executable: bool = False


@dataclass(frozen=True)
class BranchState:
    name: str
    head_sha: str
    created: bool  # False: the branch already existed and was accepted as ours


@dataclass(frozen=True)
class OpenedPullRequest:
    number: int
    url: str
    created: bool  # False: an earlier run already opened it


Call = Callable[..., Awaitable[httpx.Response]]


def commit_marker(investigation_id: str) -> str:
    """The trailer that marks a commit as made for one investigation."""
    return f"{MARKER_PREFIX} {investigation_id}"


class GitHubWriter:
    def __init__(self, settings: Settings, auth: GitHubAppAuth) -> None:
        self._base = settings.github_api_url.rstrip("/")
        self._timeout = settings.github_timeout_seconds
        self._auth = auth

    async def ensure_branch(self, target: WriteTarget, name: str, base_sha: str, investigation_id: str) -> BranchState:
        """Create ``name`` at ``base_sha``, or accept it if it already exists and is ours."""
        self._check_branch_name(target, name)
        async with self.session(target) as call:
            existing = await self._head(call, target, name)
            if existing is None:
                response = await call("POST", f"{self._repo(target)}/git/refs", json={"ref": f"refs/heads/{name}", "sha": base_sha}, expected=(201, 422))
                if response.status_code == 201:
                    return BranchState(name, base_sha, created=True)
                existing = await self._head(call, target, name)  # lost a race, or the sha is unknown to GitHub
                if existing is None:
                    raise WriteRejectedError("GitHub refused to create the branch (is the commit still in the repository?)")
            if existing != base_sha and not await self._is_ours(call, target, existing, investigation_id):
                raise BranchRefusedError("A branch with this name already exists and was not made by log0")
            return BranchState(name, existing, created=False)

    async def commit_files(
        self,
        target: WriteTarget,
        branch: str,
        base_sha: str,
        files: list[FileWrite],
        message: str,
        parent_sha: str | None = None,
    ) -> str:
        """One commit holding ``files`` on top of ``base_sha``'s tree, whose parent is ``parent_sha`` (default ``base_sha``).

        A repair builds on the first commit (the parent) but keeps the original tree as its starting point, so it
        replaces the first change instead of adding to it. Moves ``branch`` to the new commit (never forced).
        """
        if not files:
            raise WriteRejectedError("There is nothing to commit")
        self._check_branch_name(target, branch)
        async with self.session(target) as call:
            base_commit = (await call("GET", f"{self._repo(target)}/git/commits/{base_sha}")).json()
            entries = [_tree_entry(f) for f in files]
            tree = (await call("POST", f"{self._repo(target)}/git/trees", json={"base_tree": base_commit["tree"]["sha"], "tree": entries}, expected=(201,))).json()
            commit = (await call("POST", f"{self._repo(target)}/git/commits", json={"message": message, "tree": tree["sha"], "parents": [parent_sha or base_sha]}, expected=(201,))).json()
            await call("PATCH", f"{self._repo(target)}/git/refs/heads/{branch}", json={"sha": commit["sha"], "force": False})
            return str(commit["sha"])

    async def check_runs(self, target: WriteTarget, sha: str) -> list[CheckResult]:
        """The check runs (GitHub Actions and other checks apps) on a commit."""
        async with self.session(target) as call:
            response = await call("GET", f"{self._repo(target)}/commits/{sha}/check-runs", params={"per_page": "100"})
        try:
            return [
                CheckResult(
                    name=str(row["name"])[:120],
                    id=int(row["id"]) if isinstance(row.get("id"), int) else None,
                    status=row["status"] if row["status"] in ("queued", "in_progress", "completed") else "in_progress",
                    conclusion=row.get("conclusion"),
                    url=row.get("html_url"),
                )
                for row in response.json()["check_runs"]
            ]
        except (KeyError, TypeError, ValueError):
            raise GitHubUnavailableError from None

    async def job_log(self, target: WriteTarget, job_id: int, *, max_chars: int = 20000) -> str:
        """The end of a GitHub Actions job's log (needs the App's ``actions: read``). Empty text when there is none."""
        async with self.session(target) as call:
            response = await call("GET", f"{self._repo(target)}/actions/jobs/{int(job_id)}/logs", expected=(200, 302, 404, 410))
        if response.status_code in (404, 410):
            return ""  # no log (expired, or the job never ran)
        if response.status_code == 200:
            return response.text[-max_chars:]
        location = response.headers.get("location", "")
        if not _is_public_https(location):
            raise GitHubUnavailableError
        try:  # a signed download link: no credentials are sent to it
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                download = await client.get(location)
        except httpx.HTTPError:
            raise GitHubUnavailableError from None
        return download.text[-max_chars:] if download.status_code == 200 else ""

    async def head_of(self, target: WriteTarget, branch: str) -> str | None:
        """The commit a branch points at, or ``None`` when it does not exist."""
        async with self.session(target) as call:
            return await self._head(call, target, branch)

    async def open_draft_pull_request(
        self, target: WriteTarget, branch: str, title: str, body: str, labels: tuple[str, ...] = LABELS
    ) -> OpenedPullRequest:
        """Open a draft pull request from ``branch`` into the default branch, or return the one already open."""
        self._check_branch_name(target, branch)
        async with self.session(target) as call:
            found = await self._find_pull_request(call, target, branch)
            if found is not None:
                return OpenedPullRequest(found[0], found[1], created=False)
            response = await call(
                "POST",
                f"{self._repo(target)}/pulls",
                json={"title": title, "head": branch, "base": target.default_branch, "body": body, "draft": True, "maintainer_can_modify": False},
                expected=(201, 422),
            )
            if response.status_code == 422:
                found = await self._find_pull_request(call, target, branch)  # lost a race
                if found is None:
                    raise WriteRejectedError("GitHub refused to open the pull request (is there a difference between the branches?)")
                return OpenedPullRequest(found[0], found[1], created=False)
            data = response.json()
            number = int(data["number"])
            await self._label(call, target, number, labels)
            return OpenedPullRequest(number, str(data["html_url"]), created=True)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _repo(target: WriteTarget) -> str:
        return f"/repos/{target.owner}/{target.name}"

    @staticmethod
    def _check_branch_name(target: WriteTarget, name: str) -> None:
        if not name.startswith(BRANCH_PREFIX) or name == target.default_branch or ".." in name or name.endswith("/"):
            raise BranchRefusedError("log0 only writes to branches under log0/")

    async def _head(self, call: Call, target: WriteTarget, branch: str) -> str | None:
        response = await call("GET", f"{self._repo(target)}/git/ref/heads/{branch}", expected=(200, 404))
        return str(response.json()["object"]["sha"]) if response.status_code == 200 else None

    async def _is_ours(self, call: Call, target: WriteTarget, sha: str, investigation_id: str) -> bool:
        commit = (await call("GET", f"{self._repo(target)}/git/commits/{sha}")).json()
        return commit_marker(investigation_id) in str(commit.get("message", ""))

    async def _find_pull_request(self, call: Call, target: WriteTarget, branch: str) -> tuple[int, str] | None:
        response = await call("GET", f"{self._repo(target)}/pulls", params={"head": f"{target.owner}:{branch}", "state": "all"})
        rows = response.json()
        return (int(rows[0]["number"]), str(rows[0]["html_url"])) if rows else None

    async def _label(self, call: Call, target: WriteTarget, number: int, labels: tuple[str, ...]) -> None:
        try:
            await call("POST", f"{self._repo(target)}/issues/{number}/labels", json={"labels": list(labels)}, expected=(200,))
        except (GitHubAccessError, GitHubUnavailableError, WriteRejectedError) as exc:  # a label is cosmetic
            logger.info("could not label pull request %s: %s", number, type(exc).__name__)

    def session(self, target: WriteTarget, permissions: Mapping[str, str] = WRITE_PERMISSIONS) -> "_Session":
        """``async with writer.session(target) as call``: a token with ``permissions`` for one repository, for a batch of calls."""
        return _Session(self, target, permissions)


def _is_public_https(url: str) -> bool:
    """A redirect target we may fetch: https and a host name (not an address literal, not localhost)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and bool(host) and host != "localhost" and not host.replace(".", "").replace(":", "").isdigit()


def _tree_entry(file: FileWrite) -> dict[str, Any]:
    if file.content is None:
        return {"path": file.path, "mode": "100644", "type": "blob", "sha": None}
    return {"path": file.path, "mode": "100755" if file.executable else "100644", "type": "blob", "content": file.content}


class _Session:
    """``async with writer._session(target) as call``: one installation token, one HTTP client, for a batch of calls."""

    def __init__(self, writer: GitHubWriter, target: WriteTarget, permissions: Mapping[str, str]) -> None:
        self._writer = writer
        self._target = target
        self._permissions = permissions
        self._client: httpx.AsyncClient | None = None
        self._headers: Mapping[str, str] = {}

    async def __aenter__(self):
        token = await self._writer._auth.installation_token(
            self._target.installation_id, [self._target.github_repository_id], self._permissions
        )
        self._headers = api_headers(token.token.get_secret_value())
        self._client = httpx.AsyncClient(timeout=self._writer._timeout)
        return self._call

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        assert self._client is not None
        try:
            response = await self._client.request(method, f"{self._writer._base}{path}", headers=self._headers, json=json, params=params)
        except httpx.HTTPError as exc:
            logger.warning("GitHub write request failed: %s", type(exc).__name__)
            raise GitHubUnavailableError from None
        if response.status_code in expected:
            return response
        if response.status_code in (409, 422):
            raise WriteRejectedError(f"GitHub rejected the request ({response.status_code})")
        raise_for_status(response, expected[0])
        return response
