"""The pull request policy (protected files, size, rate), the kill switches, the audit, and the safety invariants."""

import dataclasses
import json
import logging
import shutil
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import text

from app.config import Settings
from app.diff.builder import FileDiff
from app.graph.nodes import pull_request as nodes
from app.integrations.github_writer import GitHubWriter
from app.publishing.github_publisher import GitHubPublisher
from app.publishing.policy import (
    PrActivity,
    PrLimits,
    check_diff,
    check_rate,
    protected_reason,
)
from app.publishing.port import PublishError
from tests.code_graph_support import Repo
from tests.pull_request_support import APPROVER, FakePublisher, reviewed_state
from tests.test_github_publisher import (  # noqa: F401  (fixture)
    BASE,
    TENANT,
    USER,
    _setup,
    factory,
)
from tests.test_github_writer import FakeAuth

needs_tools = [pytest.mark.skipif(shutil.which("git") is None, reason="git"), pytest.mark.skipif(shutil.which("rg") is None, reason="rg")]


def file(path: str, additions: int = 1, deletions: int = 1) -> FileDiff:
    return FileDiff(file_path=path, operation="modify", additions=additions, deletions=deletions)


# --------------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "svc/.github/workflows/x.yml",
        ".git/config",
        ".circleci/config.yml",
        "CODEOWNERS",
        "Jenkinsfile",
        "package-lock.json",
        "services/web/yarn.lock",
        "go.sum",
        "Cargo.lock",
        ".env",
        "config/.env.production",
        "keys/server.pem",
        "deploy/id_rsa",
        "secrets/tls.key",
        ".npmrc",
        "Credentials.JSON",
        "dir\\.github\\workflows\\ci.yml",
    ],
)
def test_files_that_change_how_code_is_released_or_authorised_are_protected(path: str) -> None:
    assert protected_reason(path) is not None


@pytest.mark.parametrize("path", ["src/payment.js", "services/payment/src/Main.java", "docs/github.md", "environment.py", "README.md", "src/keyboard.ts"])
def test_ordinary_files_are_not(path: str) -> None:
    assert protected_reason(path) is None


def test_a_change_must_stay_small_enough_to_review() -> None:
    limits = PrLimits(max_files=2, max_changed_lines=10)

    assert check_diff([file("a.py"), file("b.py")], limits) == []
    assert "3 files" in check_diff([file("a"), file("b"), file("c")], limits)[0]
    assert "12 lines" in check_diff([file("a", 6, 6)], limits)[0]


def test_every_reason_is_reported_up_to_a_few() -> None:
    reasons = check_diff([file(".github/a.yml"), file("go.sum"), file(".env"), file("x.pem")], PrLimits())

    assert len(reasons) == 3


def test_a_repository_or_workspace_cannot_be_flooded() -> None:
    limits = PrLimits(max_per_repository_per_hour=2, max_per_tenant_per_hour=5)

    assert check_rate(PrActivity(1, 4), limits) == []
    assert "this repository" in check_rate(PrActivity(2, 2), limits)[0]
    assert "workspace" in check_rate(PrActivity(0, 5), limits)[0]
    assert len(check_rate(PrActivity(2, 5), limits)) == 2


# --------------------------------------------------------------------- the graph


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("policy") / "repo")


async def offer(repo: Repo, publisher: FakePublisher, *, files: list[FileDiff] | None = None, limits: PrLimits | None = None) -> dict[str, Any]:
    state, ctx, _ = await reviewed_state(repo, publisher)
    if files is not None:
        state = state | {"diff_files": files}
    if limits is not None:
        ctx = dataclasses.replace(ctx, pr_limits=limits)
    return await nodes.offer_pr(state, type("R", (), {"context": ctx})())


@pytest.mark.asyncio
@pytest.mark.parametrize("marks", [needs_tools])
async def test_a_protected_file_means_no_offer_and_a_gap_that_keeps_the_diff_as_the_fix(repo: Repo, marks: Any) -> None:
    result = await offer(repo, FakePublisher(), files=[file(".github/workflows/deploy.yml")])

    assert result["pr_offer"] is None
    assert "No pull request was offered" in result["gaps"][0] and ".github/workflows/deploy.yml" in result["gaps"][0]


@pytest.mark.asyncio
async def test_an_oversized_change_is_not_offered(repo: Repo) -> None:
    result = await offer(repo, FakePublisher(), limits=PrLimits(max_changed_lines=1))

    assert result["pr_offer"] is None and "lines" in result["gaps"][0]


@pytest.mark.asyncio
async def test_a_flooded_repository_is_not_offered_another_pull_request(repo: Repo) -> None:
    publisher = FakePublisher()
    publisher.recent = PrActivity(repository_last_hour=3, tenant_last_hour=3)

    result = await offer(repo, publisher)

    assert result["pr_offer"] is None and "last hour" in result["gaps"][0]


@pytest.mark.asyncio
async def test_a_normal_change_is_offered_and_the_activity_was_looked_at(repo: Repo) -> None:
    publisher = FakePublisher()

    result = await offer(repo, publisher)

    assert result["pr_offer"]["gate"] == "pr_approval" and "activity" in publisher.names()


@pytest.mark.asyncio
async def test_the_commit_step_checks_the_policy_again_even_if_an_offer_was_forged(repo: Repo) -> None:
    state, ctx, _ = await reviewed_state(repo, FakePublisher())
    state = state | {"diff_files": [file(".github/workflows/deploy.yml")], "branch": object(), "commit_sha": None}
    state["branch"] = __import__("app.schemas.domain", fromlist=["BranchRef"]).BranchRef(name="log0/fix-a-b", base_sha="a" * 40)

    result = await nodes.apply_patch(state, type("R", (), {"context": ctx})())

    assert "not allowed in a pull request" in result["gaps"][0] and "commit" not in ctx.publisher.names()


# ----------------------------------------------------- invariants: no write without a yes


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [None, {"action": "decline"}, {"action": "approve"}, {"action": "approve", "decided_by": "x"}])
async def test_nothing_is_written_unless_an_engineer_approved_with_an_id(repo: Repo, decision: Any) -> None:
    from app.schemas.domain import PrDecision

    publisher = FakePublisher()
    state, ctx, _ = await reviewed_state(repo, publisher)
    parsed = nodes.read_pr_decision(decision)
    state = state | {"pr_decision": parsed if parsed else (PrDecision.model_validate(decision) if isinstance(decision, dict) and decision.get("action") else None)}
    runtime = type("R", (), {"context": ctx})()

    from app.graph.routing import after_pr_approval

    if after_pr_approval(state) == "create_branch":
        await nodes.create_branch(state, runtime)
    written = {"create_branch", "commit", "open_pull_request"} & set(publisher.names())
    approved = bool(parsed and parsed.action == "approve")
    assert bool(written) == approved


# ---------------------------------------------------------- kill switches and activity


@pytest.mark.asyncio
async def test_an_operator_can_switch_one_workspace_off_without_touching_the_others(factory) -> None:  # noqa: F811
    _, investigation_id = await _setup(factory)
    on = GitHubPublisher(factory, object(), enabled=True)  # type: ignore[arg-type]
    off = GitHubPublisher(factory, object(), enabled=True, disabled_tenants=frozenset({TENANT}))  # type: ignore[arg-type]

    target = await on.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    assert await off.target_for(tenant_id=TENANT, repo="acme/shop") is None
    with pytest.raises(PublishError, match="switched off"):
        await off.check_results(tenant_id=TENANT, target=target, commit_sha=BASE)
    assert investigation_id


@pytest.mark.asyncio
async def test_activity_counts_recent_pull_requests_of_the_repository_and_the_workspace(factory) -> None:  # noqa: F811
    repository_id, investigation_id = await _setup(factory)
    publisher = GitHubPublisher(factory, object(), enabled=True)  # type: ignore[arg-type]
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    assert await publisher.activity(tenant_id=TENANT, target=target) == PrActivity(0, 0)

    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO ai.pull_request (tenant_id, investigation_id, repository_id, branch, base_sha, approved_by) "
                "VALUES (:t, :i, :r, 'log0/fix-a-b', 'a', :u)"
            ),
            {"t": TENANT, "i": investigation_id, "r": repository_id, "u": USER},
        )
        await session.commit()

    assert await publisher.activity(tenant_id=TENANT, target=target) == PrActivity(1, 1)


# ------------------------------------------------- the token never leaks (11.5)


@pytest.mark.asyncio
@respx.mock
async def test_the_installation_token_appears_only_in_authorization_headers(factory, caplog: pytest.LogCaptureFixture) -> None:  # noqa: F811
    from app.schemas.domain import BranchRef

    repository_id, investigation_id = await _setup(factory)
    auth = FakeAuth()
    publisher = GitHubPublisher(factory, GitHubWriter(Settings(_env_file=None), auth), enabled=True)  # type: ignore[call-arg, arg-type]
    target = await publisher.target_for(tenant_id=TENANT, repo="acme/shop")
    assert target is not None
    repo_url = "https://api.github.com/repos/Acme/Shop"
    routes = [
        respx.get(f"{repo_url}/git/ref/heads/log0/fix-aaaaaaaa-bbbbbbbb").mock(return_value=httpx.Response(404)),
        respx.post(f"{repo_url}/git/refs").mock(return_value=httpx.Response(201, json={})),
        respx.get(f"{repo_url}/pulls").mock(return_value=httpx.Response(200, json=[])),
        respx.post(f"{repo_url}/pulls").mock(return_value=httpx.Response(500, text="boom ghs_write_token")),
    ]
    branch = BranchRef(name="log0/fix-aaaaaaaa-bbbbbbbb", base_sha=BASE)

    with caplog.at_level(logging.DEBUG):
        await publisher._writer.ensure_branch(await publisher._resolve(TENANT, target), branch.name, BASE, str(investigation_id))
        with pytest.raises(PublishError) as caught:
            await publisher.open_pull_request(tenant_id=TENANT, target=target, investigation_id=investigation_id, branch=branch, commit_sha="c" * 40, title="t", body="b", labels=[])

    assert "ghs_write_token" not in caplog.text and "ghs_write_token" not in str(caught.value)
    for route in routes:
        for call in route.calls:
            assert "ghs_write_token" not in (call.request.content or b"").decode() and "ghs_write_token" not in str(call.request.url)
    async with factory() as session:
        audit = (await session.execute(text("SELECT string_agg(detail || target || action, ' ') FROM ai.write_audit WHERE tenant_id = :t"), {"t": TENANT})).scalar() or ""
    assert "ghs_write_token" not in audit
    assert json.dumps({"id": str(repository_id)})
    assert APPROVER


# -------------------------------------------------- per-workspace rules (stricter only)


def test_a_workspace_can_make_the_limits_stricter_but_never_looser() -> None:
    from app.publishing.policy import TenantRules, effective_limits

    server = PrLimits(max_files=10, max_changed_lines=400, max_per_repository_per_hour=3, max_per_tenant_per_hour=10)

    assert effective_limits(server, None) == server
    assert effective_limits(server, TenantRules()) == server  # nothing chosen: the server's values
    assert effective_limits(server, TenantRules(max_files=2, max_changed_lines=50)) == PrLimits(2, 50, 3, 10)
    assert effective_limits(server, TenantRules(max_files=99, max_per_tenant_per_hour=99)) == server  # asking for more changes nothing


def test_requiring_green_checks_can_be_turned_on_by_either_side_and_off_by_neither() -> None:
    from app.publishing.policy import TenantRules, effective_only_on_green

    assert effective_only_on_green(False, None) is False
    assert effective_only_on_green(False, TenantRules(only_on_green=True)) is True
    assert effective_only_on_green(True, TenantRules(only_on_green=False)) is True
    assert effective_only_on_green(True, None) is True


@pytest.mark.asyncio
async def test_the_executor_builds_each_runs_rules_from_the_server_and_the_workspace(factory) -> None:  # noqa: F811
    from app.config import Settings
    from app.graph.executor import GraphExecutor
    from app.publishing.policy import TenantRules
    from app.store import pr_policy as policy_store

    settings = Settings(_env_file=None, pr_max_files=10, pr_only_on_green=False)  # type: ignore[call-arg]
    executor = GraphExecutor(object(), logs=object(), incidents=object(), history=object(), models=object(), settings=settings)  # type: ignore[arg-type]

    async with factory() as session:
        before = await executor._policy_for(factory, TENANT)
        await policy_store.upsert(session, tenant_id=TENANT, rules=TenantRules(only_on_green=True, max_files=3), updated_by=USER)
    after = await executor._policy_for(factory, TENANT)
    async with factory() as session:
        await policy_store.delete_for(session, tenant_id=TENANT)

    assert before["pr_limits"].max_files == 10 and before["pr_only_on_green"] is False
    assert after["pr_limits"].max_files == 3 and after["pr_only_on_green"] is True
