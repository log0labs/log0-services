"""DbCodeAccess: the real tables, the real secret box, the real cache and a local git remote."""

import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings, get_settings
from app.db.session import create_session_factory
from app.graph.code_access import CodeAccessError
from app.integrations.code_access import DbCodeAccess
from app.integrations.repo_cache import RepoCache
from app.runner import SessionFactory
from app.security.crypto import SecretBox, secret_context
from app.store import repositories as store
from tests.jwt_helpers import TENANT_A, TENANT_B, USER_ID
from tests.test_repo_cache import Remote, _git

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
    pytest.mark.asyncio,
]

TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
SERVICE = "payment-service"
KEY = Fernet.generate_key().decode()


@pytest_asyncio.fixture
async def sessions() -> AsyncGenerator[SessionFactory]:
    engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
    factory = create_session_factory(engine)

    async def wipe() -> None:
        async with factory() as session:
            for tenant in (TENANT_A, TENANT_B):
                await session.execute(text("DELETE FROM ai.service_repository WHERE tenant_id = :t"), {"t": tenant})
                await session.execute(text("DELETE FROM ai.repository_connection WHERE tenant_id = :t"), {"t": tenant})
            await session.commit()

    await wipe()
    try:
        yield factory
    finally:
        await wipe()
        await engine.dispose()


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "trunk", cwd=work)
    bare = tmp_path / "remote" / "acme" / "shop.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", "-b", "trunk", str(bare), cwd=tmp_path)
    r = Remote(base=tmp_path / "remote", work=work, bare=bare)
    (work / "services" / "payment").mkdir(parents=True)
    r.commit("services/payment/app.py", "print('one')\n")
    return r


def make_access(sessions: SessionFactory, remote: Remote, tmp_path: Path, *, box: SecretBox | None = None, fresh: float = 60) -> DbCodeAccess:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        repo_cache_dir=str(tmp_path / "cache"),
        repo_clone_base_url=remote.base.as_uri(),
        git_allowed_protocols="file",
        repo_fresh_seconds=fresh,
    )
    return DbCodeAccess(sessions, box if box is not None else SecretBox({"k1": KEY}, "k1"), RepoCache(settings))


async def connect(sessions: SessionFactory, *, tenant: UUID = TENANT_A, name: str = "shop", token: str = TOKEN, key: str = KEY, service: str | None = SERVICE, prefix: str = "services/payment") -> UUID:
    """What the 3.3 endpoints do: store the repository with an encrypted token, and map a service to it."""
    box = SecretBox({"k1": key}, "k1")
    encrypted = box.encrypt(token, context=secret_context(tenant, "repository_connection", f"acme/{name}"))
    async with sessions() as session:
        row = await store.create(session, tenant_id=tenant, owner="acme", name=name, default_branch="trunk", token=encrypted, created_by=USER_ID)
        if service:
            await store.upsert_service(session, tenant_id=tenant, service_name=service, repository_id=row.id, path_prefix=prefix)
    return row.id


# ----------------------------------------------------------------------- the happy path


async def test_a_mapped_service_opens_a_checkout_with_the_pinned_commit_and_scoped_tools(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    repo_id = await connect(sessions)
    head = _git("rev-parse", "HEAD", cwd=remote.work)

    checkout = await make_access(sessions, remote, tmp_path).open(tenant_id=TENANT_A, service_name=SERVICE)

    assert checkout is not None
    assert (checkout.repo.repo, checkout.repo.branch, checkout.repo.sha, checkout.repo.path_prefix) == ("acme/shop", "trunk", head, "services/payment")
    assert checkout.code.root == tmp_path.joinpath("cache", str(TENANT_A), str(repo_id)).resolve()
    assert (await checkout.code.list_files()).files == ["services/payment/app.py"]  # scoped to the service folder
    assert (await checkout.git.recent_commits()).commits[0].subject == "change services/payment/app.py"


async def test_a_new_commit_is_picked_up_when_the_checkout_is_stale(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions)
    access = make_access(sessions, remote, tmp_path, fresh=0)
    first = await access.open(tenant_id=TENANT_A, service_name=SERVICE)
    new_head = remote.commit("services/payment/app.py", "print('two')\n")

    second = await access.open(tenant_id=TENANT_A, service_name=SERVICE)

    assert first is not None and second is not None
    assert second.repo.sha == new_head != first.repo.sha


# ------------------------------------------------------------------------ not mapped


async def test_a_service_with_no_mapping_is_none_and_nothing_is_cloned(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions, service=None)  # connected, but no service points at it
    access = make_access(sessions, remote, tmp_path)
    assert await access.open(tenant_id=TENANT_A, service_name=SERVICE) is None
    assert await access.open(tenant_id=TENANT_A, service_name="Payment-Service") is None  # names match exactly
    assert not (tmp_path / "cache").exists()


async def test_another_tenants_mapping_is_invisible(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions, tenant=TENANT_A)
    access = make_access(sessions, remote, tmp_path)

    assert await access.open(tenant_id=TENANT_B, service_name=SERVICE) is None
    assert not (tmp_path / "cache" / str(TENANT_B)).exists()  # tenant B never caused a clone of A's repository


# ----------------------------------------------------------------------- cannot open


async def test_without_a_master_key_the_repository_cannot_be_used(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions)
    settings = Settings(_env_file=None, repo_cache_dir=str(tmp_path / "cache"), repo_clone_base_url=remote.base.as_uri(), git_allowed_protocols="file")  # type: ignore[call-arg]
    access = DbCodeAccess(sessions, None, RepoCache(settings))
    with pytest.raises(CodeAccessError, match="secret storage is not configured"):
        await access.open(tenant_id=TENANT_A, service_name=SERVICE)


async def test_a_token_that_no_longer_decrypts_asks_to_reconnect_and_leaks_nothing(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions, key=Fernet.generate_key().decode())  # encrypted with a key the service does not have
    with pytest.raises(CodeAccessError, match="reconnect the repository") as caught:
        await make_access(sessions, remote, tmp_path).open(tenant_id=TENANT_A, service_name=SERVICE)
    assert TOKEN not in str(caught.value)


async def test_a_token_moved_to_another_tenants_row_does_not_decrypt(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    """The context binding of 3.2 at work: B's connection holding A's ciphertext is refused."""
    await connect(sessions, tenant=TENANT_A, name="shop", service=None)
    async with sessions() as session:
        stolen = (await session.execute(text("SELECT token_ciphertext, key_id FROM ai.repository_connection WHERE tenant_id = :t"), {"t": TENANT_A})).one()
        await session.execute(
            text(
                "INSERT INTO ai.repository_connection (tenant_id, owner, name, default_branch, token_ciphertext, key_id, created_by) "
                "VALUES (:t, 'acme', 'shop', 'trunk', :c, :k, :u)"
            ),
            {"t": TENANT_B, "c": stolen.token_ciphertext, "k": stolen.key_id, "u": USER_ID},
        )
        repo_b = (await session.execute(text("SELECT id FROM ai.repository_connection WHERE tenant_id = :t"), {"t": TENANT_B})).scalar_one()
        await store.upsert_service(session, tenant_id=TENANT_B, service_name=SERVICE, repository_id=repo_b, path_prefix="")

    with pytest.raises(CodeAccessError, match="reconnect the repository"):
        await make_access(sessions, remote, tmp_path).open(tenant_id=TENANT_B, service_name=SERVICE)


async def test_a_repository_that_cannot_be_fetched_is_an_access_error_without_the_token(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions, name="missing-repo")  # nothing at <remote>/acme/missing-repo.git
    with pytest.raises(CodeAccessError, match="could not be fetched") as caught:
        await make_access(sessions, remote, tmp_path).open(tenant_id=TENANT_A, service_name=SERVICE)
    assert TOKEN not in str(caught.value)


# --------------------------------------------------------------------------- snapshot


async def test_a_snapshot_is_the_pinned_commit_even_after_the_branch_moved_on(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions)
    access = make_access(sessions, remote, tmp_path, fresh=0)
    opened = await access.open(tenant_id=TENANT_A, service_name=SERVICE)
    assert opened is not None
    pinned = opened.repo.sha
    remote.commit("services/payment/app.py", "print('two')\n")
    moved = await access.open(tenant_id=TENANT_A, service_name=SERVICE)  # the shared checkout follows the branch
    assert moved is not None and moved.repo.sha != pinned

    async with access.snapshot(tenant_id=TENANT_A, service_name=SERVICE, sha=pinned) as snapshot:
        assert snapshot is not None
        assert (snapshot.repo.sha, snapshot.repo.repo, snapshot.repo.path_prefix) == (pinned, "acme/shop", "services/payment")
        assert (await snapshot.code.read_file("services/payment/app.py")).text.strip().endswith("print('one')")  # the old text
        assert (await snapshot.code.list_files()).files == ["services/payment/app.py"]  # scoped to the service folder
        root = snapshot.code.root
    assert not root.exists()  # removed when the block ended


async def test_a_snapshot_of_an_unmapped_service_or_another_tenant_is_none_and_clones_nothing(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions, tenant=TENANT_A)
    access = make_access(sessions, remote, tmp_path)
    sha = "a" * 40
    async with access.snapshot(tenant_id=TENANT_A, service_name="other-service", sha=sha) as nothing:
        assert nothing is None
    async with access.snapshot(tenant_id=TENANT_B, service_name=SERVICE, sha=sha) as not_theirs:
        assert not_theirs is None
    assert not (tmp_path / "cache").exists()


async def test_a_snapshot_is_cleaned_up_when_the_block_raises(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions)
    access = make_access(sessions, remote, tmp_path)
    opened = await access.open(tenant_id=TENANT_A, service_name=SERVICE)
    assert opened is not None
    seen: list[Path] = []
    with pytest.raises(RuntimeError, match="boom"):
        async with access.snapshot(tenant_id=TENANT_A, service_name=SERVICE, sha=opened.repo.sha) as snapshot:
            assert snapshot is not None
            seen.append(snapshot.code.root)
            raise RuntimeError("boom")  # an error inside the block is not turned into a CodeAccessError
    assert not seen[0].exists()


async def test_a_commit_that_is_gone_is_an_access_error(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions)
    access = make_access(sessions, remote, tmp_path)
    await access.open(tenant_id=TENANT_A, service_name=SERVICE)
    with pytest.raises(CodeAccessError, match="no longer available"):
        async with access.snapshot(tenant_id=TENANT_A, service_name=SERVICE, sha="deadbeef" * 5):
            pytest.fail("the block must not run")


async def test_a_cleared_cache_is_cloned_again_before_the_snapshot(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    import shutil

    await connect(sessions)
    access = make_access(sessions, remote, tmp_path)
    opened = await access.open(tenant_id=TENANT_A, service_name=SERVICE)
    assert opened is not None
    shutil.rmtree(opened.code.root, onexc=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))  # someone deleted the cache folder

    async with access.snapshot(tenant_id=TENANT_A, service_name=SERVICE, sha=opened.repo.sha) as snapshot:
        assert snapshot is not None and (await snapshot.code.list_files()).files == ["services/payment/app.py"]


async def test_a_snapshot_that_needs_the_token_but_has_no_master_key_is_an_access_error(sessions: SessionFactory, remote: Remote, tmp_path: Path) -> None:
    await connect(sessions)
    settings = Settings(_env_file=None, repo_cache_dir=str(tmp_path / "cache"), repo_clone_base_url=remote.base.as_uri(), git_allowed_protocols="file")  # type: ignore[call-arg]
    access = DbCodeAccess(sessions, None, RepoCache(settings))  # the cache is empty, so a clone would be needed
    with pytest.raises(CodeAccessError, match="secret storage is not configured"):
        async with access.snapshot(tenant_id=TENANT_A, service_name=SERVICE, sha="a" * 40):
            pytest.fail("the block must not run")
