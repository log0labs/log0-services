"""The live pull request evaluation, tested offline: the scripted model and a fake GitHub drive the same harness.

These tests prove the harness scores a perfect run as perfect and a bad run as bad, like the V1 self-test. They say nothing about
log0's quality. Needs a clone of log0-demo-shop (``DEMO_SHOP_REPO``); without it they skip.
"""

import os
import re
from pathlib import Path
from typing import Any

import pytest

from app.integrations.github_writer import (
    BranchState,
    FileWrite,
    OpenedPullRequest,
    WriteTarget,
)
from app.publishing.ci import CiWait
from app.schemas.domain import CheckResult
from evals import pr_run
from evals.cases import load_cases
from evals.pr_run import EvalRepo, PrCaseResult, markdown, rate, run_pr_case, summarize
from evals.run import DEFAULT_REPO
from evals.scripted import ScriptedModels

REPO = Path(os.getenv("DEMO_SHOP_REPO", str(DEFAULT_REPO)))
pytestmark = [
    pytest.mark.skipif(not (REPO / ".git").exists(), reason="a clone of log0-demo-shop is needed (DEMO_SHOP_REPO)"),
    pytest.mark.asyncio,
]

CASES = {c.id: c for c in load_cases()}
TARGET = EvalRepo("acme", "shop-eval", 42, 7)


class FakeWriter:
    """GitHub in memory: remembers branches and commits, answers the checks from a script."""

    def __init__(self, checks: list[list[CheckResult]], log: str = "FAIL: TestSomething\nexpected 1, got 2") -> None:
        self.checks = checks
        self.log = log
        self.branches: dict[str, str] = {}
        self.commits: list[dict[str, Any]] = []
        self.pulls: list[dict[str, Any]] = []

    async def ensure_branch(self, target: WriteTarget, name: str, base_sha: str, investigation_id: str) -> BranchState:
        assert name.startswith("log0/") and name != target.default_branch  # the writer's own rule, mirrored
        self.branches[name] = base_sha
        return BranchState(name, base_sha, created=True)

    async def head_of(self, target: WriteTarget, branch: str) -> str | None:
        return self.branches.get(branch)

    async def commit_files(self, target: WriteTarget, branch: str, base_sha: str, files: list[FileWrite], message: str, parent_sha: str | None = None) -> str:
        sha = f"{len(self.commits) + 1:040d}"
        self.commits.append({"branch": branch, "files": [f.path for f in files], "parent": parent_sha or base_sha, "sha": sha})
        self.branches[branch] = sha
        return sha

    async def open_draft_pull_request(self, target: WriteTarget, branch: str, title: str, body: str, labels: tuple[str, ...] = ()) -> OpenedPullRequest:
        self.pulls.append({"branch": branch, "base": target.default_branch, "title": title, "body": body, "labels": labels})
        return OpenedPullRequest(len(self.pulls), f"https://github.com/acme/shop-eval/pull/{len(self.pulls)}", created=True)

    async def check_runs(self, target: WriteTarget, sha: str) -> list[CheckResult]:
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]

    async def job_log(self, target: WriteTarget, job_id: int, **_kw: Any) -> str:
        return self.log


def green() -> list[CheckResult]:
    return [CheckResult(name="payments", id=1, status="completed", conclusion="success")]


def red() -> list[CheckResult]:
    return [CheckResult(name="payments", id=2, status="completed", conclusion="failure")]


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.graph.nodes import pull_request as nodes

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(nodes, "_sleep", instant)


async def run(case_id: str, mode: str, writer: FakeWriter) -> PrCaseResult:
    case = CASES[case_id]
    return await run_pr_case(
        case,
        ScriptedModels(case, mode),
        clone=REPO,
        eval_repo=TARGET,
        writer=writer,  # type: ignore[arg-type]
        ref_for=lambda c: c.repo_ref,  # the isolated branch of the demo shop itself stands in for the evaluation repository's
        ci_wait=CiWait(total_seconds=1, poll_seconds=1, start_grace_seconds=0),
    )


async def test_a_perfect_run_opens_a_green_draft_pull_request_against_the_cases_own_branch() -> None:
    writer = FakeWriter([green()])

    result = await run("bug-05-retry-storm", "oracle", writer)

    assert result.error is None and result.pull_request_url == "https://github.com/acme/shop-eval/pull/1"
    assert (result.ci_first, result.ci_final, result.repaired) == ("passed", "passed", False)
    assert result.score("pr_opened") == 1 and result.score("ci_green_first_try") == 1 and result.score("ci_green_final") == 1
    assert result.score("pull_request_safe") == 1 and result.score("repair_helped") is None
    assert result.score("fix_pattern") == 1 and result.score("localization_hit@1") == 1  # the V1 metrics are carried along
    (pull,) = writer.pulls
    assert pull["base"] == CASES["bug-05-retry-storm"].repo_ref and pull["branch"].startswith("log0/fix-")


async def test_a_failing_run_of_the_checks_is_reported_and_a_repair_that_changes_nothing_is_refused() -> None:
    writer = FakeWriter([red()])  # fails every time

    result = await run("bug-05-retry-storm", "oracle", writer)

    assert (result.ci_first, result.ci_final) == ("failed", "failed")
    assert result.score("pr_opened") == 1 and result.score("ci_green_final") == 0 and result.score("ci_green_first_try") == 0
    assert len(writer.commits) == 1  # the scripted model proposes the same change again: log0 refuses to commit it twice
    assert any("same as the one that failed" in gap for gap in result.gaps)


async def test_a_bad_investigation_opens_nothing_and_scores_zero() -> None:
    writer = FakeWriter([green()])

    result = await run("bug-05-retry-storm", "wrong", writer)

    assert result.pull_request_url is None and writer.pulls == [] and writer.commits == []
    assert result.score("pr_opened") == 0 and result.score("ci_green_final") is None


async def test_a_cause_outside_the_code_must_not_get_a_pull_request() -> None:
    writer = FakeWriter([green()])

    result = await run("bug-06-key-rotation", "oracle", writer)

    assert result.score("no_pull_request_for_non_code_cause") == 1 and result.score("pr_opened") is None
    assert writer.pulls == []


async def test_the_summary_and_the_report_state_the_sample_sizes_and_the_caveats() -> None:
    good = await run("bug-05-retry-storm", "oracle", FakeWriter([green()]))
    bad = await run("bug-04-nil-map", "wrong", FakeWriter([green()]))

    summary = summarize([good, bad], label="scripted oracle and wrong")
    report = markdown(summary, [good, bad])

    assert summary["rates"]["pr_opened"] == {"hits": 1, "of": 2} and summary["cases"] == 2
    assert rate([good, bad], "ci_green_final") == (1, 1)
    assert re.search(r"A draft pull request opened .* \| 1 of 2 \|", report) and "Caveats: one run" in report
    assert "[link](https://github.com/acme/shop-eval/pull/1)" in report and "| none |" in report


async def test_the_command_line_refuses_to_write_anywhere_without_a_yes(capsys: pytest.CaptureFixture[str]) -> None:
    assert pr_run.main([]) == 2
    assert "--yes" in capsys.readouterr().err
    assert pr_run.main(["--yes", "--clone", str(REPO / "does-not-exist")]) == 2
    assert pr_run.main(["--yes", "--clone", str(REPO), "--cases", "nope"]) == 2
