"""The production publisher: write rules, bookkeeping and audit (real Postgres, fake GitHub writer)."""

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.tables import github_installation, investigation, repository_connection
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_writer import (
    BranchRefusedError,
    BranchState,
    FileWrite,
    OpenedPullRequest,
    WriteRejectedError,
)
from app.publishing.github_publisher import GitHubPublisher
from app.publishing.port import PublishError
from app.schemas.domain import BranchRef, CheckResult, CiRun
from app.store import pull_requests, write_audit

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

TENANT = UUID("34000000-0000-4000-8000-00000000000a")
OTHER = UUID("34000000-0000-4000-8000-00000000000b")
USER = UUID("34000000-0000-4000-8000-0000000000c1")
BASE = "a" * 40
NEW = "b" * 40


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.head: str | None = BASE
        self.error: Exception | None = None

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.error is not None:
            raise self.error

    async def ensure_branch(self, target: Any, name: str, base_sha: str, investigation_id: str) -> BranchState:
        self._call("ensure_branch", target, name, base_sha, investigation_id)
        return BranchState(name, base_sha, created=True)

    async def check_runs(self, target: Any, sha: str) -> list[CheckResult]:
        self._call("check_runs", target, sha)
        return [CheckResult(name="ci", status="completed", conclusion="success")]

    async def head_of(self, target: Any, branch: str) -> str | None:
        self._call("head_of", target, branch)
        return self.head

    async def commit_files(self, target: Any, branch: str, base_sha: str, files: list[FileWrite], message: str, parent_sha: str | None = None) -> str:
        self._call("commit_files", target, branch, base_sha, files, message)
        return NEW

    async def open_draft_pull_request(self, target: Any, branch: str, title: str, body: str, labels: tuple[str, ...] = ()) -> OpenedPullRequest:
        self._call("open_draft_pull_request", target, branch, title, body, labels)
        return OpenedPullRequest(8, "https://github.com/acme/shop/pull/8", created=True)


@pytest_asyncio.fixture
async def factory(db_session: AsyncSession) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async def wipe() -> None:
        for name in ("write_audit", "pull_request", "investigation", "repository_connection", "github_installation"):
            await db_session.execute(text(f"DELETE FROM ai.{name} WHERE tenant_id IN (:a, :b)"), {"a": TENANT, "b": OTHER})
        await db_session.commit()

    await wipe()
    yield async_sessionmaker(db_session.bind, expire_on_commit=False)
    await db_session.rollback()
    await wipe()


async def _setup(factory: async_sessionmaker[AsyncSession], *, tenant: UUID = TENANT, writes: bool = True, suspended: bool = False, installed: bool = True) -> tuple[UUID, UUID]:
    async with factory() as session:
        if installed:
            await session.execute(
                insert(github_installation).values(
                    tenant_id=tenant,
                    installation_id=555,
                    account_login="acme",
                    account_type="Organization",
                    suspended_at=datetime.now(UTC) if suspended else None,
                    created_by=USER,
                )
            )
        repository_id = (
            await session.execute(
                insert(repository_connection)
                .values(
                    tenant_id=tenant,
                    owner="Acme",
                    name="Shop",
                    default_branch="main",
                    token_ciphertext="x",
                    key_id="k1",
                    created_by=USER,
                    installation_id=555 if installed else None,
                    github_repository_id=7 if installed else None,
                    pr_writes_enabled=writes,
                )
                .returning(repository_connection.c.id)
            )
        ).scalar_one()
        investigation_id = (
            await session.execute(
                insert(investigation).values(tenant_id=tenant, incident_id=uuid4(), requested_by=USER, status="RUNNING").returning(investigation.c.id)
            )
        ).scalar_one()
        await session.commit()
    return repository_id, investigation_id


def _publisher(factory: async_sessionmaker[AsyncSession], writer: FakeWriter | None = None, *, enabled: bool = True) -> tuple[GitHubPublisher, FakeWriter]:
    writer = writer or FakeWriter()
    return GitHubPublisher(factory, writer, enabled=enabled), writer  # type: ignore[arg-type]


async def _audit(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str, str]]:
    async with factory() as session:
        return [(e.action, e.result, e.detail) for e in reversed(await write_audit.list_for_tenant(session, tenant_id=TENANT))]


# --------------------------------------------------------------------- target_for


async def test_a_repository_with_writes_on_and_an_active_installation_is_a_target(factory) -> None:
    repository_id, _ = await _setup(factory)
    publisher, _ = _publisher(factory)

    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")  # case does not matter

    assert target is not None and target.repository_id == repository_id and target.repo == "Acme/Shop" and target.default_branch == "main"


@pytest.mark.parametrize(
    ("setup", "enabled"),
    [({"writes": False}, True), ({"suspended": True}, True), ({"installed": False}, True), ({}, False)],
    ids=["repository switch off", "suspended installation", "no installation", "global kill switch"],
)
async def test_writing_is_refused_unless_every_switch_is_on(factory, setup: dict, enabled: bool) -> None:
    await _setup(factory, **setup)
    publisher, _ = _publisher(factory, enabled=enabled)

    assert await publisher.target_for(tenant_id=TENANT, repo="acme/shop") is None


async def test_another_tenants_repository_is_never_a_target(factory) -> None:
    await _setup(factory)
    publisher, _ = _publisher(factory)

    assert await publisher.target_for(tenant_id=OTHER, repo="acme/shop") is None
    assert await publisher.target_for(tenant_id=TENANT, repo="acme/other") is None


# ---------------------------------------------------------------------- the writes


async def test_the_three_writes_are_made_recorded_and_audited(factory) -> None:
    repository_id, investigation_id = await _setup(factory)
    publisher, writer = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None

    branch = await publisher.create_branch(tenant_id=TENANT, target=target, investigation_id=investigation_id, incident_id=uuid4(), base_sha=BASE, approved_by=USER)
    sha = await publisher.commit(tenant_id=TENANT, target=target, investigation_id=investigation_id, branch=branch, files=[FileWrite("a.py", "x")], message="m")
    pr = await publisher.open_pull_request(tenant_id=TENANT, target=target, investigation_id=investigation_id, branch=branch, commit_sha=sha, title="t", body="b", labels=["log0", "log0:tests-passing"])

    assert branch.name.startswith("log0/fix-") and branch.base_sha == BASE and sha == NEW
    assert (pr.number, pr.url, pr.branch, pr.commit_sha, pr.draft) == (8, "https://github.com/acme/shop/pull/8", branch.name, NEW, True)
    async with factory() as session:
        row = await pull_requests.get_for_investigation(session, tenant_id=TENANT, investigation_id=investigation_id)
    assert row is not None and (row.state, row.commit_sha, row.number, row.approved_by, row.repository_id) == ("open", NEW, 8, USER, repository_id)
    assert await _audit(factory) == [("branch.create", "ok", ""), ("commit.create", "ok", ""), ("pull_request.open", "ok", "")]
    assert writer.calls[0][1][0].installation_id == 555 and writer.calls[0][1][0].github_repository_id == 7


async def test_a_commit_that_already_exists_on_the_branch_is_reused_not_repeated(factory) -> None:
    _, investigation_id = await _setup(factory)
    publisher, writer = _publisher(factory)
    writer.head = NEW  # an earlier attempt already moved the branch
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    branch = BranchRef(name="log0/fix-aaaaaaaa-bbbbbbbb", base_sha=BASE)

    sha = await publisher.commit(tenant_id=TENANT, target=target, investigation_id=investigation_id, branch=branch, files=[FileWrite("a.py", "x")], message="m")

    assert sha == NEW and "commit_files" not in [name for name, _ in writer.calls]


async def test_starting_the_same_branch_twice_keeps_one_row(factory) -> None:
    _, investigation_id = await _setup(factory)
    publisher, _ = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    incident = uuid4()

    for _ in range(2):
        await publisher.create_branch(tenant_id=TENANT, target=target, investigation_id=investigation_id, incident_id=incident, base_sha=BASE, approved_by=USER)

    async with factory() as session:
        count = (await session.execute(text("SELECT count(*) FROM ai.pull_request WHERE investigation_id = :i"), {"i": investigation_id})).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (BranchRefusedError("A branch with this name already exists and was not made by log0"), "was not made by log0"),
        (WriteRejectedError("GitHub rejected the request (422)"), "GitHub rejected the request"),
        (GitHubAccessError("forbidden"), "still installed with write access"),
        (GitHubAccessError("not_found"), "was the app uninstalled"),
        (GitHubUnavailableError(), "GitHub could not be reached"),
    ],
)
async def test_failures_become_safe_messages_and_an_error_audit_row(factory, error: Exception, message: str) -> None:
    _, investigation_id = await _setup(factory)
    publisher, writer = _publisher(factory)
    writer.error = error
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None

    with pytest.raises(PublishError) as caught:
        await publisher.create_branch(tenant_id=TENANT, target=target, investigation_id=investigation_id, incident_id=uuid4(), base_sha=BASE, approved_by=USER)

    assert message in str(caught.value)
    (entry,) = await _audit(factory)
    assert entry[:2] == ("branch.create", "error") and message in entry[2]


async def test_a_switch_turned_off_after_the_target_was_found_stops_the_next_write(factory) -> None:
    repository_id, investigation_id = await _setup(factory)
    publisher, writer = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    async with factory() as session:
        await session.execute(text("UPDATE ai.repository_connection SET pr_writes_enabled = false WHERE id = :i"), {"i": repository_id})
        await session.commit()

    with pytest.raises(PublishError, match="no longer allowed"):
        await publisher.create_branch(tenant_id=TENANT, target=target, investigation_id=investigation_id, incident_id=uuid4(), base_sha=BASE, approved_by=USER)
    assert writer.calls == []


async def test_a_target_of_another_tenant_cannot_be_written_to(factory) -> None:
    _, investigation_id = await _setup(factory)
    publisher, writer = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None

    with pytest.raises(PublishError):
        await publisher.create_branch(tenant_id=OTHER, target=target, investigation_id=investigation_id, incident_id=uuid4(), base_sha=BASE, approved_by=USER)
    assert writer.calls == []


async def test_the_audit_never_holds_a_token_or_file_content(factory) -> None:
    _, investigation_id = await _setup(factory)
    publisher, _ = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    branch = BranchRef(name="log0/fix-a-b", base_sha=BASE)

    await publisher.commit(tenant_id=TENANT, target=target, investigation_id=investigation_id, branch=branch, files=[FileWrite("a.py", "SECRET-CONTENT")], message="SECRET-MESSAGE")

    async with factory() as session:
        rows = await write_audit.list_for_tenant(session, tenant_id=TENANT)
    assert "SECRET" not in repr(rows)


async def test_checks_are_read_for_the_commit_and_the_run_is_kept(factory) -> None:
    _, investigation_id = await _setup(factory)
    publisher, writer = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None

    checks = await publisher.check_results(tenant_id=TENANT, target=target, commit_sha=NEW)
    await publisher.record_ci_run(tenant_id=TENANT, investigation_id=investigation_id, commit_sha=NEW, run=CiRun(outcome="passed", checks=checks))

    assert [c.name for c in checks] == ["ci"] and writer.calls[-1][0] == "check_runs"
    async with factory() as session:
        row = (await session.execute(text("SELECT outcome, checks, commit_sha FROM ai.test_run WHERE investigation_id = :i"), {"i": investigation_id})).one()
    assert row[0] == "passed" and row[1][0]["name"] == "ci" and row[2] == NEW


async def test_reading_checks_is_refused_when_writing_is_no_longer_allowed(factory) -> None:
    repository_id, _ = await _setup(factory)
    publisher, writer = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    async with factory() as session:
        await session.execute(text("UPDATE ai.repository_connection SET pr_writes_enabled = false WHERE id = :i"), {"i": repository_id})
        await session.commit()

    with pytest.raises(PublishError, match="no longer allowed"):
        await publisher.check_results(tenant_id=TENANT, target=target, commit_sha=NEW)
    assert writer.calls == []


# ------------------------------------------------------------ repair: reading why the checks failed


def _fail(check_id: int | None, name: str = "ci", conclusion: str = "failure") -> CheckResult:
    return CheckResult(name=name, id=check_id, status="completed", conclusion=conclusion)


async def test_only_failed_checks_with_an_id_are_read_and_the_text_is_cleaned(factory) -> None:
    from app.publishing.github_publisher import clean_log

    _, _ = await _setup(factory)
    publisher, writer = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    read: list[int] = []

    async def job_log(_target: Any, job_id: int, **_kw: Any) -> str:
        read.append(job_id)
        return "2026-11-15T10:00:00.1234567Z \x1b[31mFAIL: TestX\x1b[0m\r\n2026-11-15T10:00:01.0Z token ghp_0123456789abcdefghijklmnopqrstuvwxyz"

    writer.job_log = job_log  # type: ignore[attr-defined]

    text = await publisher.ci_failure_log(
        tenant_id=TENANT, target=target, checks=[_fail(1), _fail(None, "no id"), _fail(2, "passed one", "success"), _fail(3, "lint", "timed_out"), _fail(4, "third failure")]
    )

    assert read == [1, 3]  # at most two, only failures, only ones with an id
    assert "### ci" in text and "FAIL: TestX" in text and "### lint" in text
    assert "\x1b" not in text and "2026-11-15T" not in text and "ghp_0123" not in text  # no colours, timestamps or secrets
    assert clean_log("a" * 9000).endswith("a") and len(clean_log("a" * 9000)) <= 4000


async def test_a_log_that_cannot_be_read_is_skipped_not_fatal(factory) -> None:
    _, _ = await _setup(factory)
    publisher, writer = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None

    async def forbidden(*_a: Any, **_k: Any) -> str:
        raise GitHubAccessError("forbidden")

    writer.job_log = forbidden  # type: ignore[attr-defined]

    assert await publisher.ci_failure_log(tenant_id=TENANT, target=target, checks=[_fail(1)]) == ""


async def test_the_second_commit_of_a_repair_is_made_on_the_first_and_an_earlier_one_is_reused(factory) -> None:
    _, investigation_id = await _setup(factory)
    publisher, writer = _publisher(factory)
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    branch = BranchRef(name="log0/fix-aaaaaaaa-bbbbbbbb", base_sha=BASE)
    calls: list[Any] = []
    real = writer.commit_files

    async def spy(target_: Any, branch_: str, base: str, files: list[FileWrite], message: str, parent_sha: str | None = None) -> str:
        calls.append(parent_sha)
        return await real(target_, branch_, base, files, message)

    writer.commit_files = spy  # type: ignore[method-assign]
    writer.head = NEW  # the branch already holds the first commit

    again = await publisher.commit(tenant_id=TENANT, target=target, investigation_id=investigation_id, branch=branch, files=[FileWrite("a.py", "x")], message="m", parent_sha=NEW)
    assert again == NEW and calls == [NEW]  # the branch is at the parent: this is the new commit, built on it

    writer.head = "e" * 40  # a crash after the repair commit: the branch has moved on from the parent
    reused = await publisher.commit(tenant_id=TENANT, target=target, investigation_id=investigation_id, branch=branch, files=[FileWrite("a.py", "x")], message="m", parent_sha=NEW)
    assert reused == "e" * 40 and calls == [NEW]  # reused, not committed again
