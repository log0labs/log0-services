"""Run golden cases through the real investigation graph, on fixture backends, and score them.

``run_case`` builds the same ``InvestigationContext`` production builds, with the recorded fixtures instead of
ClickHouse, the incident database and GitHub, runs the compiled graph (every node is the production node), and
reduces the final state to an ``Outcome`` for the evaluators. Which model answers is the caller's choice: a real
provider (``ChatModels``) or the scripted stand-in (harness self-test only).
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from app.graph.builder import GRAPH_VERSION, build_graph
from app.graph.state import ChatModels, InvestigationContext, new_state
from app.graph.usage import Prices, UsageRecorder, run_config
from app.retrieval.embeddings import (
    HashingEmbedder,  # noqa: F401  (re-exported for callers that build retrieval)
)
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from evals.cases import Case, RetrievalCase
from evals.evaluators import (
    Metric,
    Outcome,
    evaluate,
    retrieval_scores,
    tests_pass_after,
)
from evals.fixtures import (
    EVAL_TENANT,
    FixtureHistory,
    FixtureIncidents,
    FixtureLogs,
    FixtureRunbooks,
    RepoCode,
    load_fixture,
)
from evals.judge import Judge

SERVICES = {"storefront-api": "services/storefront-api", "payments": "services/payments", "inventory": "services/inventory"}
CASE_TIMEOUT_SECONDS = 900


@dataclass
class CaseResult:
    case: Case
    outcome: Outcome
    metrics: list[Metric] = field(default_factory=list)

    def score(self, name: str) -> float | None:
        return next((m.score for m in self.metrics if m.name == name), None)


def repo_files(repo: Path, ref: str) -> list[str]:
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref], cwd=repo, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def resolve_repo_path(hint: str, files: list[str], service_prefix: str) -> str | None:
    """Map a path from a stack trace (``/app/src/pricing.js``, ``/src/store.go``, ``./src/src/main.rs``) to a file of the
    repository: the longest trailing part of the path that names exactly one file, preferring the service's folder."""
    parts = [p for p in hint.replace("\\", "/").split("/") if p and p != "."]
    for start in range(len(parts)):
        suffix = "/".join(parts[start:])
        matches = [f for f in files if f == suffix or f.endswith("/" + suffix)]
        local = [f for f in matches if f.startswith(service_prefix + "/")]
        if len(local) == 1:
            return local[0]
        if len(matches) == 1:
            return matches[0]
    return None


def build_outcome(final: dict, recorder: UsageRecorder, code: RepoCode, case: Case, files: list[str], seconds: float) -> Outcome:
    prefix = SERVICES[case.service]
    frames = []
    for frame in final.get("stack_frames", []):
        if frame.in_app and frame.path_hint:
            frames.append(resolve_repo_path(frame.path_hint, files, prefix) or frame.path_hint)
    triage = final.get("triage")
    failed = final.get("error") if final.get("status") == InvestigationStatus.FAILED else None
    cost = recorder.cost_usd() if recorder.has_usage else None
    return Outcome(
        triage_category=triage.category if triage else None,
        root_cause=final.get("root_cause"),
        recommendation=final.get("recommendation"),
        confidence_band=final.get("confidence_band"),
        diff=final.get("diff"),
        evidence=list(final.get("evidence", [])),
        gaps=list(final.get("gaps", [])),
        failed=failed,
        tool_calls=int(final.get("tool_calls", 0)),
        code_reads=list(code.reads),
        trace_frames=list(dict.fromkeys(frames)),
        input_tokens=recorder.input_tokens,
        output_tokens=recorder.output_tokens,
        cost_usd=float(cost) if cost is not None else None,
        seconds=seconds,
    )


async def run_case(
    case: Case,
    models: ChatModels,
    *,
    repo: Path,
    prices: Prices | None = None,
    judge: Judge | None = None,
    run_tests: bool = False,
) -> CaseResult:
    incident, logs, history = load_fixture(case.fixture_dir)
    code = RepoCode(repo, case.repo_ref, SERVICES)
    files = repo_files(repo, case.repo_ref)
    context = InvestigationContext(
        tenant_id=EVAL_TENANT,
        logs=FixtureLogs(logs, inject=case.inject),  # type: ignore[arg-type]
        incidents=FixtureIncidents(incident, inject=case.inject),  # type: ignore[arg-type]
        history=FixtureHistory(history),  # type: ignore[arg-type]
        models=models,
        code=code,
        require_review=False,
    )
    state = new_state(investigation_id=str(uuid4()), incident_id=incident["incidentId"], scope=InvestigationScope(), question=case.question)
    recorder = UsageRecorder(prices)
    started = time.monotonic()
    try:
        async with asyncio.timeout(CASE_TIMEOUT_SECONDS):
            final = await build_graph().ainvoke(state, run_config(UUID(state["investigation_id"]), recorder, GRAPH_VERSION), context=context)  # type: ignore[arg-type]
        outcome = build_outcome(final, recorder, code, case, files, time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001  # a crashed run is a result (score 0 on "completed"), not a crashed evaluation
        outcome = Outcome(failed=f"{type(exc).__name__}: {exc}"[:300], seconds=time.monotonic() - started, code_reads=list(code.reads))
    finally:
        code.close()

    metrics = evaluate(case, outcome)
    if judge is not None and outcome.failed is None:
        verdict = await judge.judge(case, outcome)
        metrics = [verdict if m.name == "root_cause" else m for m in metrics]
    if run_tests:
        metrics.append(await asyncio.to_thread(tests_pass_after, case, outcome.diff, repo))
    return CaseResult(case, outcome, metrics)


# ------------------------------------------------------------------------- retrieval


@dataclass
class RetrievalResult:
    case: RetrievalCase
    retrieved: list[str]
    precision: float
    recall: float


async def run_retrieval(cases: list[RetrievalCase], runbooks_dir: Path) -> list[RetrievalResult]:
    """Retrieval alone (embed, nearest, MMR), against labelled chunks. No model, no database."""
    runbooks = FixtureRunbooks(runbooks_dir)
    results = []
    for case in cases:
        passages = await runbooks.retrieve(case.query)
        retrieved = [f"{p.title} > {p.heading_path}" for p in passages]
        precision, recall = retrieval_scores(case.relevant, retrieved)
        results.append(RetrievalResult(case, retrieved, precision, recall))
    return results
