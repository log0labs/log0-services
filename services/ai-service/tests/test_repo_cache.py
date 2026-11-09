"""The repository cache, against a real git and a local "remote" folder (no network, no GitHub).

A bare repository at ``<tmp>/remote/acme/shop.git`` plays GitHub; the cache clones it through a
``file://`` URL (the tests allow that protocol, production allows only https).
"""

import asyncio
import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.integrations import repo_cache
from app.integrations.repo_cache import (
    CheckoutError,
    RepoCache,
    scrub,
    token_for_connection,
)
from app.security.crypto import DecryptionError, SecretBox, secret_context
from app.store.repositories import RepositoryConnection

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.asyncio,
]

_real_run = subprocess.run  # the tests' own git calls must not be recorded by the spy
TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")
TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, **GIT_IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return _real_run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@dataclass
class Remote:
    base: Path  # the folder the cache treats as "https://github.com"
    work: Path  # a working copy used to make commits
    bare: Path

    def commit(self, name: str, text: str) -> str:
        (self.work / name).write_text(text, encoding="utf-8")
        _git("add", name, cwd=self.work)
        _git("commit", "-m", f"change {name}", cwd=self.work)
        _git("push", str(self.bare), "trunk", cwd=self.work)
        return _git("rev-parse", "HEAD", cwd=self.work)


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "trunk", cwd=work)
    bare = tmp_path / "remote" / "acme" / "shop.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", "-b", "trunk", str(bare), cwd=tmp_path)
    r = Remote(base=tmp_path / "remote", work=work, bare=bare)
    r.commit("app.py", "print('one')\n")
    return r


def _settings(tmp_path: Path, remote: Remote, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "repo_cache_dir": str(tmp_path / "cache"),
        "repo_clone_base_url": remote.base.as_uri(),
        "git_allowed_protocols": "file",
    }
    return Settings(_env_file=None, **(values | overrides))  # type: ignore[call-arg, arg-type]


def _connection(*, tenant: UUID = TENANT, name: str = "shop", branch: str = "trunk", **overrides: object) -> RepositoryConnection:
    values: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": tenant,
        "provider": "github",
        "owner": "acme",
        "name": name,
        "default_branch": branch,
        "token_ciphertext": "not-used-here",
        "key_id": "k1",
        "created_by": uuid4(),
        "created_at": datetime.now(tz=UTC),
    }
    return RepositoryConnection(**(values | overrides))  # type: ignore[arg-type]


@pytest.fixture
def cache(tmp_path: Path, remote: Remote) -> RepoCache:
    return RepoCache(_settings(tmp_path, remote))


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str], Path]]:
    """Records every git command the cache runs (command, environment, cwd) and runs it for real."""
    calls: list[tuple[list[str], dict[str, str], Path]] = []

    def recording(command, **kwargs):
        calls.append((list(command), dict(kwargs["env"]), kwargs["cwd"]))
        return _real_run(command, **kwargs)

    monkeypatch.setattr(repo_cache.subprocess, "run", recording)
    return calls


def _verbs(calls: list[tuple[list[str], dict[str, str], Path]]) -> list[str]:
    return [c[0][next(i for i, a in enumerate(c[0][1:], 1) if not a.startswith("-") and a != "core.symlinks=false")] for c in calls]


SECRET = SecretStr(TOKEN)


# --------------------------------------------------------------------------- cloning


async def test_the_first_call_clones_the_default_branch_and_reports_the_commit(cache: RepoCache, remote: Remote, tmp_path: Path) -> None:
    connection = _connection()
    head = _git("rev-parse", "HEAD", cwd=remote.work)

    checkout = await cache.ensure_checkout(connection, SECRET)

    assert checkout.sha == head and checkout.branch == "trunk"
    assert (checkout.path / "app.py").read_text(encoding="utf-8").strip() == "print('one')"
    assert checkout.path == tmp_path.joinpath("cache", str(TENANT), str(connection.id)).resolve()
    assert _git("rev-parse", "--is-shallow-repository", cwd=checkout.path) == "true"  # one commit, not the history


async def test_the_path_holds_only_ids_never_names_a_user_typed(cache: RepoCache) -> None:
    connection = _connection()
    path = cache.checkout_path(connection)
    assert path.parts[-2:] == (str(TENANT), str(connection.id))
    assert "acme" not in str(path) and "shop" not in path.name


async def test_different_tenants_and_repositories_get_different_folders(cache: RepoCache) -> None:
    a, b, c = _connection(), _connection(), _connection(tenant=OTHER_TENANT)
    assert len({cache.checkout_path(x) for x in (a, b, c)}) == 3


# ------------------------------------------------------------------ the token's journey


async def test_the_token_is_never_in_a_command_line_and_reaches_git_only_through_the_environment(
    cache: RepoCache, spy: list[tuple[list[str], dict[str, str], Path]]
) -> None:
    await cache.ensure_checkout(_connection(), SECRET)

    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert spy and all(TOKEN not in part and encoded not in part for command, _, _ in spy for part in command)
    clone_env = next(env for command, env, _ in spy if "clone" in command)
    assert clone_env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert clone_env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {encoded}"


async def test_the_token_is_not_stored_anywhere_in_the_checkout(cache: RepoCache) -> None:
    checkout = await cache.ensure_checkout(_connection(), SECRET)
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    for file in checkout.path.rglob("*"):
        if file.is_file():
            data = file.read_bytes()
            assert TOKEN.encode() not in data and encoded.encode() not in data and b"extraheader" not in data.lower(), file
    config = (checkout.path / ".git" / "config").read_text(encoding="utf-8")
    assert "acme/shop.git" in config and "Authorization" not in config


async def test_git_runs_with_a_minimal_locked_down_environment(
    cache: RepoCache, spy: list[tuple[list[str], dict[str, str], Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-pass-me-on")
    await cache.ensure_checkout(_connection(), SECRET)

    for _, env, _ in spy:
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_ALLOW_PROTOCOL"] == "file"
        # nothing else from the service's own environment (API keys, database URL, ...) is handed to git
        assert "AWS_SECRET_ACCESS_KEY" not in env and "GROQ_API_KEY" not in env and "JWT_SECRET" not in env


async def test_production_allows_only_https_by_default() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.git_allowed_protocols == "https" and settings.repo_clone_base_url == "https://github.com"


async def test_the_checkout_has_no_hooks_and_does_not_materialise_symlinks(cache: RepoCache) -> None:
    checkout = await cache.ensure_checkout(_connection(), SECRET)
    hooks = checkout.path / ".git" / "hooks"
    assert not hooks.exists() or list(hooks.iterdir()) == []  # --template= : nothing is copied in
    assert "symlinks = false" in (checkout.path / ".git" / "config").read_text(encoding="utf-8")


# ------------------------------------------------------------------------ keeping fresh


async def test_a_recent_checkout_is_reused_without_asking_the_remote(
    cache: RepoCache, spy: list[tuple[list[str], dict[str, str], Path]]
) -> None:
    connection = _connection()
    first = await cache.ensure_checkout(connection, SECRET)
    spy.clear()

    second = await cache.ensure_checkout(connection, SECRET)  # within REPO_FRESH_SECONDS (60)

    assert second == first
    assert "fetch" not in _verbs(spy) and "clone" not in _verbs(spy)


async def test_a_stale_checkout_is_updated_and_local_changes_are_discarded(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    first = await cache.ensure_checkout(connection, SECRET)
    (first.path / "app.py").write_text("tampered\n", encoding="utf-8")  # a local edit
    (first.path / "junk.txt").write_text("not in the repository\n", encoding="utf-8")
    new_head = remote.commit("app.py", "print('two')\n")

    second = await cache.ensure_checkout(connection, SECRET, max_age_seconds=0)

    assert second.sha == new_head != first.sha
    assert (second.path / "app.py").read_text(encoding="utf-8").strip() == "print('two')"
    assert not (second.path / "junk.txt").exists()


async def test_an_update_needs_no_history_only_the_new_tip(cache: RepoCache, remote: Remote, spy: list[tuple[list[str], dict[str, str], Path]]) -> None:
    connection = _connection()
    await cache.ensure_checkout(connection, SECRET)
    remote.commit("b.py", "x = 1\n")
    spy.clear()
    await cache.ensure_checkout(connection, SECRET, max_age_seconds=0)
    assert _verbs(spy) == ["fetch", "reset", "clean", "rev-parse"]
    fetch = spy[0][0]
    assert "--depth" in fetch and fetch[fetch.index("--depth") + 1] == "1"


async def test_a_folder_that_is_not_a_checkout_is_replaced_by_a_clean_clone(cache: RepoCache) -> None:
    connection = _connection()
    path = cache.checkout_path(connection)
    path.mkdir(parents=True)
    (path / "leftover.txt").write_text("from a crashed run\n", encoding="utf-8")

    checkout = await cache.ensure_checkout(connection, SECRET)

    assert (checkout.path / "app.py").exists() and not (checkout.path / "leftover.txt").exists()


# ---------------------------------------------------------------------------- locking


async def test_concurrent_requests_for_one_repository_clone_it_once(
    cache: RepoCache, spy: list[tuple[list[str], dict[str, str], Path]]
) -> None:
    connection = _connection()

    results = await asyncio.gather(*(cache.ensure_checkout(connection, SECRET) for _ in range(6)))

    assert len({r.sha for r in results}) == 1 and len({r.path for r in results}) == 1
    assert _verbs(spy).count("clone") == 1  # the others waited, found it fresh and reused it


async def test_different_repositories_do_not_wait_for_each_other(cache: RepoCache) -> None:
    a, b = _connection(), _connection(tenant=OTHER_TENANT)
    first, second = await asyncio.gather(cache.ensure_checkout(a, SECRET), cache.ensure_checkout(b, SECRET))
    assert first.path != second.path and first.sha == second.sha  # same remote content, two separate copies


# ------------------------------------------------------------------------- failures


async def test_a_missing_repository_is_a_clean_error_and_leaves_nothing_behind(cache: RepoCache, tmp_path: Path) -> None:
    connection = _connection(name="does-not-exist")

    with pytest.raises(CheckoutError, match="clone failed"):
        await cache.ensure_checkout(connection, SECRET)

    assert not cache.checkout_path(connection).exists()
    assert list((tmp_path / "cache" / str(TENANT)).iterdir()) == []  # no staging folder either


async def test_a_missing_branch_is_a_clean_error(cache: RepoCache) -> None:
    with pytest.raises(CheckoutError, match="clone failed"):
        await cache.ensure_checkout(_connection(branch="no-such-branch"), SECRET)


async def test_a_git_that_takes_too_long_is_killed_and_reported(tmp_path: Path, remote: Remote) -> None:
    slow = RepoCache(_settings(tmp_path, remote, git_timeout_seconds=0.001))
    with pytest.raises(CheckoutError, match="timed out"):
        await slow.ensure_checkout(_connection(), SECRET)
    assert list((tmp_path / "cache" / str(TENANT)).iterdir()) == []


async def test_a_missing_git_binary_is_explained(tmp_path: Path, remote: Remote) -> None:
    broken = RepoCache(_settings(tmp_path, remote, git_binary="git-does-not-exist"))
    with pytest.raises(CheckoutError, match="installed"):
        await broken.ensure_checkout(_connection(), SECRET)


async def test_errors_never_contain_the_token(cache: RepoCache, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG"), pytest.raises(CheckoutError) as caught:
        await cache.ensure_checkout(_connection(name="does-not-exist"), SECRET)
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert TOKEN not in str(caught.value) and encoded not in str(caught.value)
    assert TOKEN not in caplog.text and encoded not in caplog.text


async def test_scrub_removes_the_token_and_its_encoded_form() -> None:
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    text = f"fatal: header Authorization: Basic {encoded} for {TOKEN} rejected"
    cleaned = scrub(text, TOKEN)
    assert TOKEN not in cleaned and encoded not in cleaned and cleaned.count("***") == 2
    assert scrub("nothing secret", "") == "nothing secret"


# -------------------------------------------------------------------- hostile values


@pytest.mark.parametrize("branch", ["", "-x", "--upload-pack=evil", "a..b", "a b", "a;b", "x" * 256, "a\nb"])
async def test_an_unusable_branch_name_never_reaches_git(cache: RepoCache, spy: list[tuple[list[str], dict[str, str], Path]], branch: str) -> None:
    with pytest.raises(CheckoutError, match="branch"):
        await cache.ensure_checkout(_connection(branch=branch), SECRET)
    assert spy == []


@pytest.mark.parametrize(("owner", "name"), [("../x", "shop"), ("acme", ".."), ("ac/me", "shop"), ("acme", "sh op"), ("", "shop")])
async def test_an_unusable_owner_or_name_never_reaches_git(
    cache: RepoCache, spy: list[tuple[list[str], dict[str, str], Path]], owner: str, name: str
) -> None:
    with pytest.raises(CheckoutError, match="owner or name"):
        await cache.ensure_checkout(_connection(owner=owner, name=name), SECRET)
    assert spy == []


# ----------------------------------------------------------------- stored token helper


async def test_the_stored_token_decrypts_for_its_own_tenant_and_repository_only() -> None:
    from cryptography.fernet import Fernet

    box = SecretBox({"k1": Fernet.generate_key().decode()}, "k1")
    stored = box.encrypt(TOKEN, context=secret_context(TENANT, "repository_connection", "acme/shop"))
    mine = _connection(token_ciphertext=stored.ciphertext, key_id=stored.key_id)

    assert token_for_connection(mine, box).get_secret_value() == TOKEN

    for stolen in (_connection(tenant=OTHER_TENANT, token_ciphertext=stored.ciphertext, key_id=stored.key_id), _connection(name="other", token_ciphertext=stored.ciphertext, key_id=stored.key_id)):
        with pytest.raises(DecryptionError):
            token_for_connection(stolen, box)
