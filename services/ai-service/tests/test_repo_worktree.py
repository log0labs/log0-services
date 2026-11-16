"""RepoCache.worktree: a private copy of the repository at one commit, however far the shared checkout moves."""

import asyncio
import shutil
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.integrations.repo_cache import CheckoutError, RepoCache
from tests.test_repo_cache import Remote, _connection, _git, _settings

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.asyncio,
]

SECRET = SecretStr("ghp_0123456789abcdefghijklmnopqrstuvwxyz")


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "trunk", cwd=work)
    bare = tmp_path / "remote" / "acme" / "shop.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", "-b", "trunk", str(bare), cwd=tmp_path)
    r = Remote(base=tmp_path / "remote", work=work, bare=bare)
    r.commit("app.py", "version = 1\n")
    return r


@pytest.fixture
def cache(tmp_path: Path, remote: Remote) -> RepoCache:
    return RepoCache(_settings(tmp_path, remote, repo_fresh_seconds=0))


def head(remote: Remote) -> str:
    return _git("rev-parse", "HEAD", cwd=remote.work)


def siblings(cache: RepoCache, connection) -> list[str]:
    """Everything next to the shared checkout in its tenant folder (worktrees would show up here)."""
    return sorted(p.name for p in cache.checkout_path(connection).parent.iterdir())


async def test_the_worktree_holds_the_pinned_commit_even_after_the_shared_checkout_moved_on(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    pinned = (await cache.ensure_checkout(connection, SECRET)).sha
    assert pinned == head(remote)
    remote.commit("app.py", "version = 2\n")
    moved = await cache.ensure_checkout(connection, SECRET)  # the shared checkout follows the branch...
    assert moved.sha != pinned and (moved.path / "app.py").read_text(encoding="utf-8") == "version = 2\n"

    async with cache.worktree(connection, pinned) as snapshot:  # ...the snapshot stays where the investigation was
        assert (snapshot / "app.py").read_text(encoding="utf-8") == "version = 1\n"
        assert _git("rev-parse", "HEAD", cwd=snapshot) == pinned
        assert snapshot != moved.path and snapshot.parent == moved.path.parent


async def test_a_later_update_of_the_shared_checkout_does_not_change_an_open_worktree(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    pinned = (await cache.ensure_checkout(connection, SECRET)).sha
    async with cache.worktree(connection, pinned) as snapshot:
        remote.commit("app.py", "version = 2\n")
        await cache.ensure_checkout(connection, SECRET)  # another investigation refreshes the checkout meanwhile
        assert (snapshot / "app.py").read_text(encoding="utf-8") == "version = 1\n"


async def test_the_worktree_is_removed_when_the_block_ends_and_the_shared_checkout_is_untouched(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    pinned = (await cache.ensure_checkout(connection, SECRET)).sha
    async with cache.worktree(connection, pinned) as snapshot:
        assert snapshot.exists()
    assert not snapshot.exists()
    assert siblings(cache, connection) == [str(connection.id)]  # no folder left next to the checkout
    assert _git("worktree", "list", cwd=cache.checkout_path(connection)).count("\n") == 0  # git forgot it too


async def test_the_worktree_is_removed_even_when_the_block_raises(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    pinned = (await cache.ensure_checkout(connection, SECRET)).sha
    with pytest.raises(RuntimeError, match="boom"):
        async with cache.worktree(connection, pinned) as snapshot:
            raise RuntimeError("boom")
    assert not snapshot.exists() and siblings(cache, connection) == [str(connection.id)]


async def test_the_worktree_is_removed_when_the_task_is_cancelled(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    pinned = (await cache.ensure_checkout(connection, SECRET)).sha
    entered = asyncio.Event()
    seen: list[Path] = []

    async def hold() -> None:
        async with cache.worktree(connection, pinned) as snapshot:
            seen.append(snapshot)
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(hold())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not seen[0].exists() and siblings(cache, connection) == [str(connection.id)]  # a cancelled run cleaned up


async def test_two_investigations_get_two_independent_worktrees(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    pinned = (await cache.ensure_checkout(connection, SECRET)).sha
    async with cache.worktree(connection, pinned) as first, cache.worktree(connection, pinned) as second:
        assert first != second
        (first / "scratch.txt").write_text("mine", encoding="utf-8")  # edits stay private
        assert not (second / "scratch.txt").exists()
        assert not (cache.checkout_path(connection) / "scratch.txt").exists()
    assert siblings(cache, connection) == [str(connection.id)]


async def test_simultaneous_requests_all_get_a_worktree(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    pinned = (await cache.ensure_checkout(connection, SECRET)).sha

    async def use() -> str:
        async with cache.worktree(connection, pinned) as snapshot:
            return (snapshot / "app.py").read_text(encoding="utf-8")

    assert await asyncio.gather(*(use() for _ in range(5))) == ["version = 1\n"] * 5
    assert siblings(cache, connection) == [str(connection.id)]


@pytest.mark.parametrize("bad", ["", "main", "HEAD", "abc1234", "--help", "-b", "g" * 40, "a" * 39, "a" * 41, "../x", "abc; rm -rf /"])
async def test_only_a_full_commit_id_is_accepted_so_nothing_can_be_a_ref_or_an_option(cache: RepoCache, remote: Remote, bad: str) -> None:
    connection = _connection()
    await cache.ensure_checkout(connection, SECRET)
    with pytest.raises(CheckoutError, match="40 hexadecimal"):
        async with cache.worktree(connection, bad):
            pytest.fail("the block must not run")
    assert siblings(cache, connection) == [str(connection.id)]


async def test_a_commit_that_is_not_in_the_repository_is_a_clean_error_and_leaves_nothing(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    await cache.ensure_checkout(connection, SECRET)
    with pytest.raises(CheckoutError, match="worktree failed"):
        async with cache.worktree(connection, "deadbeef" * 5):
            pytest.fail("the block must not run")
    assert siblings(cache, connection) == [str(connection.id)]


async def test_a_repository_that_is_not_checked_out_is_a_clean_error(cache: RepoCache) -> None:
    with pytest.raises(CheckoutError, match="not checked out"):
        async with cache.worktree(_connection(), "a" * 40):
            pytest.fail("the block must not run")


async def test_the_worktree_does_not_materialise_symlinks_like_the_shared_checkout(cache: RepoCache, remote: Remote) -> None:
    connection = _connection()
    pinned = (await cache.ensure_checkout(connection, SECRET)).sha
    async with cache.worktree(connection, pinned) as snapshot:
        assert _git("config", "--get", "core.symlinks", cwd=snapshot) == "false"
