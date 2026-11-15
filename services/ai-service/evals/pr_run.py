"""The live pull request evaluation:  uv run python -m evals.pr_run --yes

Runs the planted-bug cases through the real graph with fixture incidents (as ``evals.run`` does) but with the
**pull request flow switched on against a real throwaway repository**: the harness plays the engineer and approves
the offer, log0 creates a branch, commits, waits for the repository's own CI, repairs once if it fails, and opens a
draft pull request. What is measured is the thing V2 adds: how often a pull request opens, whether the checks pass
(first try and after a repair), and whether the fix has the right shape.

    uv run python -m evals.pr_run --yes --eval-repo ashmitjsg/log0-demo-shop-eval --clone ../../../log0-demo-shop-eval
    uv run python -m evals.pr_run --yes --cases bug-05-retry-storm --judge

Needs: the log0 GitHub App installed on the evaluation repository (with Actions: read for repairs), a local clone of
that repository with a branch ``eval/<bug>`` for each case (the isolated bug branch plus a CI workflow), and the
configured model. It **sends code to the model provider and writes branches and draft pull requests to the repository**,
so it refuses to start without --yes. Never point it at a repository you care about.

Read the numbers with their sample size: one run of eight cases moves a rate by 12.5 points.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.config import get_settings
from app.graph.builder import GRAPH_VERSION, build_graph
from app.graph.state import ChatModels, InvestigationContext, new_state
from app.graph.usage import Prices, UsageRecorder, run_config
from app.integrations.github_app import GitHubAppAuth
from app.integrations.github_app_client import GitHubAppClient
from app.integrations.github_writer import GitHubWriter
from app.publishing.ci import CiWait
from app.schemas.domain import CiRun
from app.schemas.investigation import InvestigationScope
from evals.cases import EVALS, Case, load_cases
from evals.eval_publisher import EvalPublisher
from evals.evaluators import Metric, Outcome, evaluate
from evals.fixtures import (
    EVAL_TENANT,
    FixtureHistory,
    FixtureIncidents,
    FixtureLogs,
    RepoCode,
    load_fixture,
)
from evals.judge import Judge, LlmJudge
from evals.run import emit, real_models
from evals.runner import CASE_TIMEOUT_SECONDS, SERVICES, build_outcome, repo_files

REPORTS = EVALS / "reports"
APPROVER = UUID("55555555-5555-4555-8555-555555555555")
DEFAULT_CLONE = EVALS.parents[3] / "log0-demo-shop-eval"
PR_TIMEOUT_SECONDS = CASE_TIMEOUT_SECONDS + 2400  # the checks of the repository take minutes, twice if a repair happens


@dataclass(frozen=True)
class EvalRepo:
    """The throwaway repository the pull requests go to, and the App installation that may write to it."""

    owner: str
    name: str
    installation_id: int
    github_repository_id: int


@dataclass
class PrCaseResult:
    case: Case
    outcome: Outcome
    metrics: list[Metric] = field(default_factory=list)
    pull_request_url: str | None = None
    ci_first: str | None = None  # the repository's checks on the first commit: passed, failed, none, timeout
    ci_final: str | None = None
    repaired: bool = False
    gaps: list[str] = field(default_factory=list)
    error: str | None = None

    def score(self, name: str) -> float | None:
        return next((m.score for m in self.metrics if m.name == name), None)


def pr_metrics(case: Case, final: dict[str, Any], publisher: EvalPublisher) -> list[Metric]:
    """What V2 adds to the V1 metrics, from the end state of the run and what the repository's checks said."""
    pull_request = final.get("pull_request")
    runs = publisher.ci_runs
    first, last = (runs[0].outcome if runs else None), (runs[-1].outcome if runs else None)
    repaired = int(final.get("repair_attempts", 0)) > 0 and len(runs) > 1
    metrics: list[Metric] = []
    if case.expected.fixable_by_code:
        metrics.append(Metric("pr_opened", 1.0 if pull_request else 0.0, pull_request.url if pull_request else "no pull request"))
        if pull_request:
            metrics.append(Metric("ci_green_first_try", 1.0 if first == "passed" else 0.0, f"first run: {first}"))
            metrics.append(Metric("ci_green_final", 1.0 if last == "passed" else 0.0, f"final run: {last}"))
            metrics.append(Metric("repair_helped", (1.0 if last == "passed" else 0.0) if repaired else None, "a repair was made" if repaired else "no repair needed or possible"))
            safe = pull_request.branch.startswith("log0/") and pull_request.draft
            metrics.append(Metric("pull_request_safe", 1.0 if safe else 0.0, f"{pull_request.branch}, draft={pull_request.draft}"))
    else:  # a cause outside the code (a rotated key): the right answer is to open nothing
        metrics.append(Metric("no_pull_request_for_non_code_cause", 0.0 if pull_request else 1.0, "opened one" if pull_request else "none opened"))
    return metrics


async def run_pr_case(
    case: Case,
    models: ChatModels,
    *,
    clone: Path,
    eval_repo: EvalRepo,
    writer: GitHubWriter,
    ref_for: Any = None,
    prices: Prices | None = None,
    judge: Judge | None = None,
    ci_wait: CiWait | None = None,
) -> PrCaseResult:
    ref = ref_for(case) if ref_for else case.repo_ref.replace("isolated/", "eval/")
    incident, logs, history = load_fixture(case.fixture_dir)
    code = RepoCode(clone, ref, SERVICES)
    files = repo_files(clone, ref)
    publisher = EvalPublisher(
        writer,
        owner=eval_repo.owner,
        name=eval_repo.name,
        installation_id=eval_repo.installation_id,
        github_repository_id=eval_repo.github_repository_id,
        base_branch=ref,
    )
    context = InvestigationContext(
        tenant_id=EVAL_TENANT,
        logs=FixtureLogs(logs, inject=case.inject),  # type: ignore[arg-type]
        incidents=FixtureIncidents(incident, inject=case.inject),  # type: ignore[arg-type]
        history=FixtureHistory(history),  # type: ignore[arg-type]
        models=models,
        code=code,
        require_review=False,
        publisher=publisher,
        ci_wait=ci_wait or CiWait(total_seconds=900, poll_seconds=15, start_grace_seconds=120),
        ci_repair_attempts=1,
    )
    state = new_state(investigation_id=str(uuid4()), incident_id=incident["incidentId"], scope=InvestigationScope(), question=case.question)
    recorder = UsageRecorder(prices)
    config = run_config(UUID(state["investigation_id"]), recorder, GRAPH_VERSION)
    graph = build_graph(InMemorySaver())
    started = time.monotonic()
    final: dict[str, Any] = {}
    error: str | None = None
    try:
        async with asyncio.timeout(PR_TIMEOUT_SECONDS):
            result = await graph.ainvoke(state, config, context=context)  # type: ignore[arg-type]
            if "__interrupt__" in result:  # the offer: the harness plays the engineer and says yes
                await graph.ainvoke(Command(resume={"action": "approve", "decided_by": str(APPROVER)}), config, context=context)  # type: ignore[arg-type]
            final = dict((await graph.aget_state(config)).values)  # type: ignore[arg-type]
        outcome = build_outcome(final, recorder, code, case, files, time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001  # a crashed case is a result, not a crashed evaluation
        error = f"{type(exc).__name__}: {exc}"[:300]
        outcome = Outcome(failed=error, seconds=time.monotonic() - started, code_reads=list(code.reads))
    finally:
        code.close()

    metrics = evaluate(case, outcome)
    if judge is not None and outcome.failed is None:
        verdict = await judge.judge(case, outcome)
        metrics = [verdict if m.name == "root_cause" else m for m in metrics]
    metrics += pr_metrics(case, final, publisher) if error is None else []
    pull_request = final.get("pull_request")
    runs: list[CiRun] = publisher.ci_runs
    return PrCaseResult(
        case=case,
        outcome=outcome,
        metrics=metrics,
        pull_request_url=pull_request.url if pull_request else None,
        ci_first=runs[0].outcome if runs else None,
        ci_final=runs[-1].outcome if runs else None,
        repaired=int(final.get("repair_attempts", 0)) > 0 and len(runs) > 1,
        gaps=list(final.get("gaps", [])),
        error=error,
    )


# ------------------------------------------------------------------------------ the report


def rate(results: list[PrCaseResult], name: str) -> tuple[int, int]:
    """(cases that scored 1, cases that were scored) for one metric."""
    scores = [s for r in results if (s := r.score(name)) is not None]
    return sum(1 for s in scores if s >= 1.0), len(scores)


def summarize(results: list[PrCaseResult], *, label: str) -> dict[str, Any]:
    names = ["pr_opened", "ci_green_first_try", "ci_green_final", "repair_helped", "pull_request_safe", "no_pull_request_for_non_code_cause", "localization_hit@1", "fix_pattern", "diff_valid"]
    degraded = [r.case.id for r in results if r.outcome.model_failures or r.error]
    return {
        "label": label,
        "graph_version": GRAPH_VERSION,
        "cases": len(results),
        "degraded": degraded,
        "rates": {n: {"hits": h, "of": total} for n in names if (total := rate(results, n)[1]) for h, _ in [rate(results, n)]},
        "per_case": [
            {
                "id": r.case.id,
                "pull_request": r.pull_request_url,
                "ci_first": r.ci_first,
                "ci_final": r.ci_final,
                "repaired": r.repaired,
                "seconds": round(r.outcome.seconds, 1),
                "tokens": r.outcome.input_tokens + r.outcome.output_tokens,
                "metrics": {m.name: m.score for m in r.metrics if m.score is not None},
                "gaps": r.gaps[:6],
                "error": r.error,
            }
            for r in results
        ],
    }


def markdown(summary: dict[str, Any], results: list[PrCaseResult]) -> str:
    lines = [f"# Live pull request evaluation ({summary['label']})", "", f"Graph `{summary['graph_version']}`, {summary['cases']} cases, one run.", ""]
    if summary["degraded"]:
        lines += [f"**Degraded (model failures or crashes, read their rows with care):** {', '.join(summary['degraded'])}", ""]
    lines += ["| Case | PR | CI first | CI final | Repaired | Fix pattern | hit@1 | Seconds |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        fix, hit = r.score("fix_pattern"), r.score("localization_hit@1")
        pr = f"[link]({r.pull_request_url})" if r.pull_request_url else "none"
        lines.append(
            f"| {r.case.id} | {pr} | {r.ci_first or '-'} | {r.ci_final or '-'} | {'yes' if r.repaired else 'no'} | "
            f"{'-' if fix is None else f'{fix:.0f}'} | {'-' if hit is None else f'{hit:.0f}'} | {r.outcome.seconds:.0f} |"
        )
    lines += ["", "| Metric | Result |", "|---|---|"]
    labels = {
        "pr_opened": "A draft pull request opened (cases with a code cause)",
        "ci_green_first_try": "The repository's checks passed on the first commit",
        "ci_green_final": "The checks passed on the final commit (after any repair)",
        "repair_helped": "A repair turned a failing run into a passing one",
        "pull_request_safe": "The branch is under log0/ and the pull request is a draft",
        "no_pull_request_for_non_code_cause": "No pull request for a cause outside the code",
        "localization_hit@1": "The right file was named first",
        "fix_pattern": "The fix has the expected shape",
        "diff_valid": "The diff applies and shows the expected change",
    }
    for name, label in labels.items():
        hits, total = rate(results, name)
        if total:
            lines.append(f"| {label} | {hits} of {total} |")
    caveats = (
        "Caveats: one run, a handful of cases, one model; a case that is a rate limit or an outage measures the provider. "
        "A passing check means the repository's own tests passed, which is only as strong as those tests. "
        "The harness plays the engineer and always approves."
    )
    lines += ["", caveats]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------- the CLI


async def discover(owner: str, name: str) -> tuple[EvalRepo, GitHubWriter]:
    """Find the installation of the log0 App on the repository and GitHub's id for it (the App must be installed there)."""
    import httpx

    from app.integrations.github import api_headers

    settings = get_settings()
    auth = GitHubAppAuth(settings)
    async with httpx.AsyncClient(timeout=20) as http:
        found = await http.get(f"{settings.github_api_url}/repos/{owner}/{name}/installation", headers=api_headers(auth.app_jwt()))
    if found.status_code != 200:
        raise SystemExit(f"the log0 GitHub App is not installed on {owner}/{name} (GitHub answered {found.status_code}); install it there first")
    installation_id = int(found.json()["id"])
    repositories = await GitHubAppClient(settings, auth).list_repositories(installation_id)
    match = next((r for r in repositories if (r.owner.lower(), r.name.lower()) == (owner.lower(), name.lower())), None)
    if match is None:
        raise SystemExit(f"the installation does not include {owner}/{name}")
    return EvalRepo(match.owner, match.name, installation_id, match.github_id), GitHubWriter(settings, auth)


async def run_all(cases: list[Case], args: argparse.Namespace) -> tuple[list[PrCaseResult], str]:
    models, label, prices = real_models(args.model)
    eval_repo, writer = await discover(*args.eval_repo.split("/", 1))
    judge = LlmJudge(models) if args.judge else None
    results = []
    for case in cases:
        started = time.monotonic()
        emit(f"  {case.id} ...")
        result = await run_pr_case(case, models, clone=args.clone, eval_repo=eval_repo, writer=writer, prices=prices, judge=judge)
        status = result.pull_request_url or result.error or "no pull request"
        emit(f"  {case.id:28s} CI {result.ci_first or '-'} -> {result.ci_final or '-'}  {status}  ({time.monotonic() - started:.0f}s)")
        results.append(result)
    return results, label


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="confirm: code goes to the model provider, branches and draft pull requests go to --eval-repo")
    parser.add_argument("--eval-repo", default="ashmitjsg/log0-demo-shop-eval", help="owner/name of the THROWAWAY repository")
    parser.add_argument("--clone", type=Path, default=DEFAULT_CLONE, help="a local clone of it, with an eval/<bug> branch per case")
    parser.add_argument("--cases", help="comma-separated case ids (default: every bug case)")
    parser.add_argument("--model", help="provider:model for every purpose (default: the service's settings)")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--output", type=Path, default=REPORTS)
    args = parser.parse_args(argv)

    if not args.yes:
        print(f"This sends code to the model provider and writes branches and draft pull requests to {args.eval_repo}. Add --yes to go ahead.", file=sys.stderr)
        return 2
    if not (args.clone / ".git").exists():
        print(f"no clone at {args.clone} (pass --clone)", file=sys.stderr)
        return 2
    cases = [c for c in load_cases() if c.kind == "bug"]
    if args.cases:
        wanted = set(args.cases.split(","))
        unknown = wanted - {c.id for c in cases}
        if unknown:
            print(f"unknown case ids: {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.id in wanted]

    emit(f"running {len(cases)} cases against {args.eval_repo} ...")
    results, label = asyncio.run(run_all(cases, args))
    summary = summarize(results, label=label)
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = markdown(summary, results)
    (args.output / f"{stamp}-pr.md").write_text(report, encoding="utf-8")
    (args.output / f"{stamp}-pr.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    emit(report)
    return 3 if summary["degraded"] else 0


if __name__ == "__main__":
    import platform

    if platform.system() == "Windows":  # psycopg's async mode cannot use Windows' default event loop
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(main())
