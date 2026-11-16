"""Reading the repository's own CI: classification, the waiting node, the pull request text and the GitHub call."""

import dataclasses
import shutil
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.graph.events import events_from_update
from app.graph.nodes import pull_request as nodes
from app.integrations.github import GitHubUnavailableError
from app.integrations.github_app import InstallationToken
from app.integrations.github_writer import GitHubWriter, WriteTarget
from app.publishing.ci import CiWait, all_completed, label_for, outcome_of
from app.publishing.naming import pull_request_body
from app.publishing.port import PublishError
from app.schemas.domain import BranchRef, CheckResult, CiRun
from tests.code_graph_support import Repo
from tests.pull_request_support import COMMIT_SHA, GOOD, FakePublisher, reviewed_state


def check(name: str = "ci / test", status: str = "completed", conclusion: str | None = "success") -> CheckResult:
    return CheckResult(name=name, status=status, conclusion=conclusion)  # type: ignore[arg-type]


# --------------------------------------------------------------------- classification


def test_checks_are_decided_only_when_every_one_has_completed() -> None:
    assert not all_completed([])
    assert not all_completed([check(), check(status="in_progress", conclusion=None)])
    assert all_completed([check(), check("lint", conclusion="skipped")])


@pytest.mark.parametrize(
    ("conclusions", "outcome"),
    [
        (["success"], "passed"),
        (["success", "neutral", "skipped"], "passed"),
        (["success", "failure"], "failed"),
        (["timed_out"], "failed"),
        (["cancelled"], "failed"),
        (["something_new"], "failed"),
        ([None], "failed"),
    ],
)
def test_one_bad_or_unknown_check_fails_the_commit(conclusions: list[str | None], outcome: str) -> None:
    assert outcome_of([check(conclusion=c) for c in conclusions]) == outcome


def test_only_a_decided_outcome_gets_a_label() -> None:
    assert label_for("passed") == "log0:tests-passing" and label_for("failed") == "log0:tests-failing"
    assert label_for("none") is None and label_for("timeout") is None


# ---------------------------------------------------------------------------- the node


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("ci") / "repo")


class Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture
def sleeper(monkeypatch: pytest.MonkeyPatch) -> Sleeper:
    fake = Sleeper()
    monkeypatch.setattr(nodes, "_sleep", fake)
    return fake


pytestmark_tools = [pytest.mark.skipif(shutil.which("git") is None, reason="git"), pytest.mark.skipif(shutil.which("rg") is None, reason="rg")]


async def run(repo: Repo, publisher: FakePublisher, *, wait: CiWait | None = None, **state_over: Any) -> tuple[dict[str, Any], FakePublisher]:
    state, ctx, _ = await reviewed_state(repo, publisher)
    ctx = dataclasses.replace(ctx, ci_wait=wait or CiWait(total_seconds=60, poll_seconds=10, start_grace_seconds=20))
    state = state | {"commit_sha": COMMIT_SHA, "branch": BranchRef(name="log0/fix-a-b", base_sha="a" * 40)} | state_over
    runtime = type("R", (), {"context": ctx})()
    return await nodes.run_tests(state, runtime), publisher


@pytest.mark.asyncio
async def test_finished_checks_decide_at_once_and_are_recorded(repo: Repo, sleeper: Sleeper) -> None:
    result, publisher = await run(repo, FakePublisher())

    assert result["ci_run"].outcome == "passed" and result["ci_run"].checks[0].name == "ci / test"
    assert sleeper.slept == [] and publisher.recorded == [result["ci_run"]]


@pytest.mark.asyncio
async def test_it_waits_while_checks_run_and_then_decides(repo: Repo, sleeper: Sleeper) -> None:
    publisher = FakePublisher()
    publisher.checks = [[check(status="queued", conclusion=None)], [check(status="in_progress", conclusion=None)], [check(conclusion="failure")]]

    result, _ = await run(repo, publisher)

    assert result["ci_run"].outcome == "failed" and sleeper.slept == [10, 10]


@pytest.mark.asyncio
async def test_a_repository_without_checks_is_none_after_the_grace_period(repo: Repo, sleeper: Sleeper) -> None:
    publisher = FakePublisher()
    publisher.checks = [[]]

    result, _ = await run(repo, publisher)

    assert result["ci_run"].outcome == "none" and result["ci_run"].checks == [] and sum(sleeper.slept) == 20


@pytest.mark.asyncio
async def test_checks_still_running_when_time_is_up_are_a_timeout(repo: Repo, sleeper: Sleeper) -> None:
    publisher = FakePublisher()
    publisher.checks = [[check(status="in_progress", conclusion=None)]]

    result, _ = await run(repo, publisher)

    assert result["ci_run"].outcome == "timeout" and sum(sleeper.slept) == 60 and result["ci_run"].checks[0].status == "in_progress"


@pytest.mark.asyncio
async def test_unreadable_checks_are_a_gap_and_the_run_goes_on(repo: Repo, sleeper: Sleeper) -> None:
    publisher = FakePublisher()
    publisher.fail["check_results"] = PublishError("GitHub could not be reached")

    result, _ = await run(repo, publisher)

    assert "ci_run" not in result and "could not be read: GitHub could not be reached" in result["gaps"][0]


@pytest.mark.asyncio
async def test_it_does_nothing_twice_or_without_a_commit(repo: Repo, sleeper: Sleeper) -> None:
    done = CiRun(outcome="passed", checks=[])
    assert (await run(repo, FakePublisher(), ci_run=done))[0] == {}
    assert (await run(repo, FakePublisher(), commit_sha=None))[0] == {}


# ------------------------------------------------------------------ the pull request


@pytest.mark.asyncio
async def test_the_pull_request_says_what_the_checks_said_and_is_labelled(repo: Repo, sleeper: Sleeper) -> None:
    state, ctx, _ = await reviewed_state(repo, FakePublisher())
    publisher = ctx.publisher
    state = state | {"commit_sha": COMMIT_SHA, "branch": BranchRef(name="log0/fix-a-b", base_sha="a" * 40), "ci_run": CiRun(outcome="failed", checks=[check(conclusion="failure")]), "pr_decision": None}
    runtime = type("R", (), {"context": ctx})()

    result = await nodes.open_draft_pr(state, runtime)

    opened = publisher.arguments("open_pull_request")
    assert result["pull_request"].number == 12 and opened["labels"] == ["log0", "log0:tests-failing"]
    assert "## Tests" in opened["body"] and "**failed**" in opened["body"] and "- ci / test: failure" in opened["body"]


@pytest.mark.asyncio
async def test_only_on_green_keeps_a_failing_change_on_its_branch_without_a_pull_request(repo: Repo) -> None:
    state, ctx, _ = await reviewed_state(repo, FakePublisher())
    ctx = dataclasses.replace(ctx, pr_only_on_green=True)
    state = state | {"commit_sha": COMMIT_SHA, "branch": BranchRef(name="log0/fix-a-b", base_sha="a" * 40), "ci_run": CiRun(outcome="timeout", checks=[])}

    result = await nodes.open_draft_pr(state, type("R", (), {"context": ctx})())

    assert "pull_request" not in result and "on branch log0/fix-a-b" in result["gaps"][0]
    assert "open_pull_request" not in ctx.publisher.names()


@pytest.mark.asyncio
async def test_only_on_green_still_opens_a_passing_or_unchecked_change(repo: Repo) -> None:
    for ci in (CiRun(outcome="passed", checks=[]), CiRun(outcome="none", checks=[]), None):
        state, ctx, _ = await reviewed_state(repo, FakePublisher())
        ctx = dataclasses.replace(ctx, pr_only_on_green=True)
        state = state | {"commit_sha": COMMIT_SHA, "branch": BranchRef(name="log0/fix-a-b", base_sha="a" * 40), "ci_run": ci}

        assert "pull_request" in await nodes.open_draft_pr(state, type("R", (), {"context": ctx})())


def test_the_body_is_honest_about_every_outcome() -> None:
    from app.schemas.domain import RootCause

    cause = RootCause(statement="x", evidence_ids=[])

    def body(ci: CiRun | None) -> str:
        return pull_request_body(root_cause=cause, confidence_band=None, changes=[GOOD], files=[], ci_run=ci)

    assert "**passed**" in body(CiRun(outcome="passed"))
    assert "nothing verified this change" in body(CiRun(outcome="none"))
    assert "still running" in body(CiRun(outcome="timeout"))
    assert "could not read the checks" in body(None)


def test_the_tests_event_carries_names_and_conclusions_only() -> None:
    run = CiRun(outcome="failed", checks=[check(conclusion="failure"), check("lint", status="in_progress", conclusion=None)])

    assert events_from_update({"run_tests": {"ci_run": run}}) == [
        ("tests.completed", {"outcome": "failed", "checks": [{"name": "ci / test", "conclusion": "failure"}, {"name": "lint", "conclusion": "in_progress"}], "source": "repository"})
    ]


# ------------------------------------------------------------------------- GitHub


class _Auth:
    async def installation_token(self, *_a: Any, **_k: Any) -> InstallationToken:
        from datetime import UTC, datetime, timedelta

        return InstallationToken(SecretStr("ghs_x"), datetime.now(UTC) + timedelta(hours=1))


TARGET = WriteTarget("acme", "shop", 1, 7, "main")
URL = f"https://api.github.com/repos/acme/shop/commits/{'c' * 40}/check-runs"


@pytest.mark.asyncio
@respx.mock
async def test_check_runs_are_read_from_the_commit() -> None:
    route = respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "check_runs": [
                    {"name": "build", "status": "completed", "conclusion": "success", "html_url": "https://github.com/acme/shop/runs/1"},
                    {"name": "weird", "status": "waiting", "conclusion": None},
                ]
            },
        )
    )
    writer = GitHubWriter(Settings(_env_file=None), _Auth())  # type: ignore[call-arg, arg-type]

    checks = await writer.check_runs(TARGET, "c" * 40)

    assert [(c.name, c.status, c.conclusion) for c in checks] == [("build", "completed", "success"), ("weird", "in_progress", None)]
    assert checks[0].url == "https://github.com/acme/shop/runs/1" and route.called


@pytest.mark.asyncio
@respx.mock
async def test_an_unreadable_check_answer_is_unavailable() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"nope": 1}))
    writer = GitHubWriter(Settings(_env_file=None), _Auth())  # type: ignore[call-arg, arg-type]

    with pytest.raises(GitHubUnavailableError):
        await writer.check_runs(TARGET, "c" * 40)
