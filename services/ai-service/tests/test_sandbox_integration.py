"""The sandbox inside the graph: when it runs, what it reports, what the pull request says, who can switch it on."""

import dataclasses
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.graph.events import events_from_update
from app.graph.nodes import pull_request as nodes
from app.graph.nodes.pull_request import _apply_files, _ci_run_from
from app.publishing.ci import CiWait
from app.publishing.naming import pull_request_body
from app.publishing.policy import TenantRules, effective_sandbox_tests
from app.publishing.port import PublishError, PublishFile
from app.sandbox.port import SandboxRun
from app.schemas.domain import BranchRef, CheckResult, CiRun, RootCause
from tests.code_graph_support import PAYMENT, Repo
from tests.pull_request_support import COMMIT_SHA, GOOD, FakePublisher, reviewed_state
from tests.test_change_proposer import NEW

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
]


class FakeSandbox:
    def __init__(self, result: SandboxRun | None | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.seen: str | None = None

    async def run(self, *, source: Path, subdir: str) -> SandboxRun | None:
        self.calls.append({"subdir": subdir})
        self.seen = (source / PAYMENT).read_text(encoding="utf-8")  # what the tests would have run against
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def finished(status: str = "passed", tail: str = "ok") -> SandboxRun:
    return SandboxRun(name="go test ./...", status=status, exit_code=0 if status == "passed" else 1, output_tail=tail, seconds=1.0)  # type: ignore[arg-type]


@pytest.fixture
def repo(tmp_path: Path) -> Repo:  # one repository per test: the fake snapshot is the repository itself
    return Repo(tmp_path / "repo")


async def run_tests(repo: Repo, sandbox: FakeSandbox | None, *, opted_in: bool = True, ci_checks: list[CheckResult] | None = None) -> tuple[dict[str, Any], FakePublisher, FakeSandbox | None]:
    publisher = FakePublisher()
    publisher.checks = [ci_checks or []]
    state, ctx, _ = await reviewed_state(repo, publisher)
    ctx = dataclasses.replace(ctx, ci_wait=CiWait(total_seconds=1, poll_seconds=1, start_grace_seconds=0), sandbox=sandbox, sandbox_tests=opted_in)
    state = state | {"commit_sha": COMMIT_SHA, "branch": BranchRef(name="log0/fix-a-b", base_sha="a" * 40)}
    return await nodes.run_tests(state, type("R", (), {"context": ctx})()), publisher, sandbox


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(nodes, "_sleep", instant)


# ------------------------------------------------------------------------ when it runs


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "outcome", "conclusion"), [("passed", "passed", "success"), ("failed", "failed", "failure"), ("timeout", "timeout", "timed_out")])
async def test_with_no_ci_an_opted_in_workspace_gets_the_sandbox_result_on_the_reviewed_change(repo: Repo, status: str, outcome: str, conclusion: str) -> None:
    sandbox = FakeSandbox(finished(status, tail="--- FAIL: TestX"))

    result, publisher, _ = await run_tests(repo, sandbox)

    run = result["ci_run"]
    assert (run.outcome, run.source) == (outcome, "sandbox")
    assert [(c.name, c.conclusion) for c in run.checks] == [("log0 sandbox: go test ./...", conclusion)]
    assert run.detail == (None if status == "passed" else "--- FAIL: TestX")
    assert NEW in (sandbox.seen or "")  # the tests ran against the change, not the old code
    assert sandbox.calls == [{"subdir": "services/payment"}] and publisher.recorded == [run]


@pytest.mark.asyncio
async def test_the_sandbox_is_not_used_when_the_repository_has_ci(repo: Repo) -> None:
    sandbox = FakeSandbox(finished())

    result, _, _ = await run_tests(repo, sandbox, ci_checks=[CheckResult(name="ci", status="completed", conclusion="success")])

    assert result["ci_run"].source == "repository" and sandbox.calls == []


@pytest.mark.asyncio
async def test_the_sandbox_is_not_used_unless_the_workspace_opted_in_or_the_server_has_one(repo: Repo) -> None:
    sandbox = FakeSandbox(finished())

    off, _, _ = await run_tests(repo, sandbox, opted_in=False)
    absent, _, _ = await run_tests(repo, None)

    assert off["ci_run"].outcome == "none" and absent["ci_run"].outcome == "none" and sandbox.calls == []


@pytest.mark.asyncio
async def test_a_toolchain_the_sandbox_does_not_know_leaves_the_honest_answer_none(repo: Repo) -> None:
    result, _, _ = await run_tests(repo, FakeSandbox(None))

    assert result["ci_run"].outcome == "none" and result["ci_run"].source == "repository" and "gaps" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [finished("error", "the sandbox could not run the tests"), PublishError("no longer connected"), OSError("disk full")])
async def test_a_broken_sandbox_is_a_gap_and_the_pull_request_still_says_nothing_verified(repo: Repo, failure: Any) -> None:
    result, _, _ = await run_tests(repo, FakeSandbox(failure))

    assert result["ci_run"].outcome == "none" and "could not" in result["gaps"][0]


# -------------------------------------------------------------- mapping and files


def test_a_sandbox_error_is_not_a_test_result() -> None:
    assert _ci_run_from(finished("error")) is None


def test_applying_the_change_refuses_to_leave_the_snapshot(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("a")
    _apply_files(tmp_path, [PublishFile("sub/new.go", "package x\n"), PublishFile("keep.txt", None)])
    assert (tmp_path / "sub" / "new.go").read_text() == "package x\n" and not (tmp_path / "keep.txt").exists()

    with pytest.raises(PublishError):
        _apply_files(tmp_path, [PublishFile("../escape.txt", "x")])


# ------------------------------------------------------------- what the PR says


def _body(ci: CiRun) -> str:
    return pull_request_body(root_cause=RootCause(statement="x", evidence_ids=[]), confidence_band=None, changes=[GOOD], files=[], ci_run=ci)


def test_the_pull_request_says_it_was_the_sandbox_not_the_ci_and_shows_the_output_of_a_failure() -> None:
    passed = _body(_ci_run_from(finished("passed")))  # type: ignore[arg-type]
    failed = _body(_ci_run_from(finished("failed", "--- FAIL: TestAdd\n```evil```")))  # type: ignore[arg-type]

    assert "isolated sandbox" in passed and "not your CI environment" in passed and "<details>" not in passed
    assert "**failed**" in failed and "<details><summary>Output</summary>" in failed and "--- FAIL: TestAdd" in failed
    assert "```evil```" not in failed  # the output cannot break out of its code block


def test_the_tests_event_says_whose_tests_they_were() -> None:
    run = _ci_run_from(finished("passed"))
    assert events_from_update({"run_tests": {"ci_run": run}})[0][1]["source"] == "sandbox"
    assert events_from_update({"run_tests": {"ci_run": CiRun(outcome="none")}})[0][1]["source"] == "repository"


# ----------------------------------------------------------------- who may switch it on


def test_the_sandbox_needs_both_a_server_that_has_one_and_a_workspace_that_asked() -> None:
    asked = TenantRules(sandbox_tests=True)

    assert effective_sandbox_tests(True, asked) is True
    assert effective_sandbox_tests(False, asked) is False  # the server decides whether code may run at all
    assert effective_sandbox_tests(True, TenantRules()) is False and effective_sandbox_tests(True, None) is False
