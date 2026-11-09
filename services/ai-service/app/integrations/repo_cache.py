"""A local, read-only copy of a tenant's repository, kept fresh: what the code tools (3.5 to 3.7) read.

``RepoCache.ensure_checkout`` clones a connected repository into
``<cache>/<tenant id>/<repository id>`` (only ids in the path: nothing a user typed) and later
calls bring it up to date. It uses the git command line, shallow (one commit, one branch).

What it is careful about, because the token is a secret and the repository is someone else's data:

* **The token never appears in a command line or on disk.** It travels as an HTTP header set
  through ``GIT_CONFIG_*`` environment variables of the git child process, so it is not in the
  process list and not written to ``.git/config``; the stored remote URL is the clean one.
* **Only https** by default (``GIT_ALLOW_PROTOCOL``), no prompts, no system or user git config,
  no templates (so no hooks), and symlinks are checked out as plain files, so a repository
  cannot plant a link that points outside its own folder.
* **Atomic**: a clone goes into a temporary folder and is renamed into place, so a crash or a
  timeout never leaves a half-made checkout at the final path.
* **One at a time per checkout**: concurrent investigations of the same repository wait on a
  lock and reuse the first one's work.
* **Errors are scrubbed**: git's output is cleaned of the token before it is logged or raised.
"""

import asyncio
import base64
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

from app.config import Settings
from app.security.crypto import SecretBox, secret_context
from app.store.repositories import RepositoryConnection

logger = logging.getLogger(__name__)

# A branch name: no leading "-" (it would be read as an option), no "..", only ordinary characters.
_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,255}$")
_NAME_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SYNC_MARKER = "log0-synced"  # touched inside .git after every successful sync
_STDERR_LIMIT = 300
_PASSTHROUGH_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR")  # git needs these; nothing else is inherited


class CheckoutError(Exception):
    """The repository could not be cloned or updated. The message is safe to log and to show."""


@dataclass(frozen=True)
class Checkout:
    path: Path
    sha: str
    branch: str


def token_for_connection(connection: RepositoryConnection, box: SecretBox) -> SecretStr:
    """Decrypt a connection's token. The context is the one 3.3 used when it was stored."""
    context = secret_context(connection.tenant_id, "repository_connection", f"{connection.owner}/{connection.name}")
    return box.decrypt(connection.token_ciphertext, key_id=connection.key_id, context=context)


def scrub(text: str, token: str) -> str:
    """Remove the token (and its encoded forms) from git's output."""
    if token:
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        text = text.replace(encoded, "***").replace(token, "***")
    return text


def _valid_name(part: str) -> bool:
    return bool(_NAME_PART.match(part)) and part not in (".", "..")  # ".." would climb out of the URL's folder


def _remove(path: Path) -> None:
    """Delete a folder tree; git marks object files read-only, which blocks deletion on Windows."""

    def make_writable_and_retry(function, target, _error) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=make_writable_and_retry)


class RepoCache:
    """Owns the cache folder. Create one per process and share it (the locks live here)."""

    def __init__(self, settings: Settings) -> None:
        self._root = Path(settings.repo_cache_dir).resolve()
        self._base_url = settings.repo_clone_base_url.rstrip("/")
        self._git = settings.git_binary
        self._timeout = settings.git_timeout_seconds
        self._protocols = settings.git_allowed_protocols
        self._fresh_seconds = settings.repo_fresh_seconds
        self._locks: dict[Path, asyncio.Lock] = {}

    def checkout_path(self, connection: RepositoryConnection) -> Path:
        return self._root / str(connection.tenant_id) / str(connection.id)

    async def ensure_checkout(
        self,
        connection: RepositoryConnection,
        token: SecretStr,
        *,
        max_age_seconds: float | None = None,
    ) -> Checkout:
        """The repository's default branch, cloned or brought up to date, and its commit sha.

        If the checkout was synced less than ``max_age_seconds`` ago (default: the
        ``REPO_FRESH_SECONDS`` setting) nothing is fetched. Raises ``CheckoutError``.
        """
        branch = connection.default_branch
        if not _BRANCH.match(branch):
            raise CheckoutError("the repository's default branch has an unusable name")
        if not all(_valid_name(part) for part in (connection.owner, connection.name)):
            raise CheckoutError("the repository's owner or name is not valid")
        url = f"{self._base_url}/{connection.owner}/{connection.name}.git"
        secret = token.get_secret_value()
        path = self.checkout_path(connection)
        max_age = self._fresh_seconds if max_age_seconds is None else max_age_seconds

        lock = self._locks.setdefault(path, asyncio.Lock())
        async with lock:  # one clone or fetch per checkout at a time
            if not (path / ".git" / "HEAD").is_file():
                await self._clone(url, branch, path, secret)
            elif not self._is_fresh(path, max_age):
                await self._update(url, branch, path, secret)
            sha = (await self._run(["rev-parse", "HEAD"], secret, cwd=path)).strip()
        return Checkout(path=path, sha=sha, branch=branch)

    # ------------------------------------------------------------------- internals

    @staticmethod
    def _is_fresh(path: Path, max_age: float) -> bool:
        marker = path / ".git" / _SYNC_MARKER
        try:
            return (time.time() - marker.stat().st_mtime) < max_age
        except OSError:
            return False

    async def _clone(self, url: str, branch: str, path: Path, secret: str) -> None:
        if path.exists():  # something is there but is not a usable checkout (a crash, a manual edit)
            await asyncio.to_thread(_remove, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f"{path.name}.tmp-{uuid4().hex[:8]}")
        try:
            await self._run(
                [
                    "-c", "core.symlinks=false",
                    "clone", "--depth", "1", "--single-branch", "--branch", branch,
                    "--no-tags", "--template=", "--", url, str(staging),
                ],
                secret,
                cwd=path.parent,
            )
            (staging / ".git" / _SYNC_MARKER).touch()
            os.replace(staging, path)  # atomic: the final path is either absent or complete
        finally:
            if staging.exists():
                await asyncio.to_thread(_remove, staging)

    async def _update(self, url: str, branch: str, path: Path, secret: str) -> None:
        # The URL is given explicitly (not "origin"), so it always comes from the current settings.
        await self._run(["fetch", "--depth", "1", "--no-tags", "--", url, branch], secret, cwd=path)
        await self._run(["reset", "--hard", "FETCH_HEAD"], secret, cwd=path)
        await self._run(["clean", "-ffdx"], secret, cwd=path)  # drop anything that is not the repository's
        (path / ".git" / _SYNC_MARKER).touch()

    def _environment(self, secret: str) -> dict[str, str]:
        env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",  # never wait for a password
                "GIT_ASKPASS": "",
                "GIT_CONFIG_NOSYSTEM": "1",  # ignore the machine's and the user's git config
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ALLOW_PROTOCOL": self._protocols,
                "LC_ALL": "C",
            }
        )
        if secret:
            credentials = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
                }
            )
        return env

    async def _run(self, args: list[str], secret: str, *, cwd: Path) -> str:
        command = [self._git, *args]
        verb = next(arg for arg in args if not arg.startswith("-") and arg != "core.symlinks=false")
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=cwd,
                env=self._environment(secret),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:  # run() kills the child before raising
            logger.warning("git %s timed out after %ss", verb, self._timeout)
            raise CheckoutError(f"git {verb} timed out") from None
        except OSError:
            logger.warning("git could not be started")
            raise CheckoutError("git could not be started (is it installed?)") from None
        if result.returncode != 0:
            detail = scrub(result.stderr or "", secret).strip()[-_STDERR_LIMIT:]
            logger.warning("git %s failed (%s): %s", verb, result.returncode, detail)
            raise CheckoutError(f"git {verb} failed ({result.returncode}): {detail}")
        return result.stdout
