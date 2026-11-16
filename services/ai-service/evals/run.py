"""Run the evaluation:  uv run python -m evals.run

    uv run python -m evals.run                                  # harness self-test: scripted model, offline, free
    uv run python -m evals.run --mode real --yes                # the configured models, on the golden set (sends data)
    uv run python -m evals.run --mode real --yes --compare groq:openai/gpt-oss-120b,openai:gpt-4.1
    uv run python -m evals.run --mode real --yes --judge --run-tests --update-baseline

Modes
  oracle | wrong | hostile   the scripted stand-in model (see scripted.py). Proves the harness works; says
                             nothing about log0's quality. This is what CI runs on every change.
  real                       real language models through the same factory the service uses (LLM_* settings).
                             It sends the fixtures' (synthetic) logs and the demo repository's code to the
                             provider, so it refuses to start without --yes.

Exit codes: 0 fine; 1 regression against the baseline; 2 bad usage; 3 some cases had model failures (a rate limit, an outage,
an unusable answer): their scores are excluded and no baseline is written from such a run.

Output: a Markdown report and a JSON summary in evals/reports/ (git-ignored except for the baseline). With
--baseline FILE (default evals/baseline.json if it exists) the run is compared with it and the exit code is 1
on a regression beyond --threshold. --update-baseline writes this run's summary as the new baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.llm.model_spec import ModelSpec
from app.llm.models import PurposeChatModels
from evals.cases import EVALS, Case, load_cases, load_retrieval_cases
from evals.judge import LlmJudge
from evals.report import comparison, markdown, regressions, summarize
from evals.runner import CaseResult, run_case, run_retrieval
from evals.scripted import ScriptedModels

if sys.platform == "win32":  # psycopg's async mode cannot use Windows' default event loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DEFAULT_REPO = EVALS.parents[3] / "log0-demo-shop"
REPORTS = EVALS / "reports"
BASELINE = EVALS / "baseline.json"
SCRIPTED = {"oracle", "wrong", "hostile", "broken"}


def emit(text: str, stream: Any = None) -> None:
    """Print text that may contain characters the console cannot encode (a judge reply with a narrow no-break space
    crashed a Windows run after hours of model calls, before the baseline was written): replace, never raise."""
    out = stream or sys.stdout
    encoding = getattr(out, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    out.write(safe if safe.endswith(chr(10)) else safe + chr(10))
    out.flush()


def real_models(spec: str | None) -> tuple[Any, str, dict[str, tuple[float, float]]]:
    """The models to evaluate: the service's own settings, or ``provider:model`` for every purpose."""
    settings = get_settings()
    label = "configured models"
    if spec:
        provider, _, model = spec.partition(":")
        if not model:
            raise SystemExit(f"--compare entries look like provider:model, got {spec!r}")
        chosen = ModelSpec(provider=provider, model=model)
        settings = settings.model_copy(update={"llm_triage": chosen, "llm_summary": chosen, "llm_investigation": chosen, "llm_evaluation": chosen})
        label = spec
    return PurposeChatModels(settings), label, dict(settings.llm_prices)


async def run_all(cases: list[Case], args: argparse.Namespace, spec: str | None = None) -> tuple[list[CaseResult], str]:
    prices: dict[str, tuple[float, float]] = {}
    label = f"scripted {args.mode} (harness self-test)"
    real = args.mode == "real"
    if real:
        models, label, prices = real_models(spec)
    results = []
    for case in cases:
        models_for_case = models if real else ScriptedModels(case, args.mode)
        judge = LlmJudge(models_for_case) if args.judge else None
        started = time.monotonic()
        result = await run_case(case, models_for_case, repo=args.repo, prices=prices, judge=judge, run_tests=args.run_tests)
        done = [m for m in result.metrics if m.score is not None]
        print(f"  {case.id:28s} {sum(m.score for m in done) / max(len(done), 1):.2f} avg over {len(done)} metrics, {time.monotonic() - started:.1f}s", flush=True)
        results.append(result)
    return results, label


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", default="oracle", choices=[*sorted(SCRIPTED), "real"])
    parser.add_argument("--yes", action="store_true", help="confirm that --mode real may send the fixtures and code to the model provider")
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="a clone of log0-demo-shop (default: the sibling folder)")
    parser.add_argument("--judge", action="store_true", help="grade the root cause with a language model instead of keywords")
    parser.add_argument("--run-tests", action="store_true", help="apply each diff and run the service's own tests (slow)")
    parser.add_argument("--compare", help="comma-separated provider:model list: run the same cases on each and print a table")
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--no-baseline", action="store_true", help="do not compare with a baseline, even if evals/baseline.json exists")
    parser.add_argument("--threshold", type=float, default=0.05)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--output", type=Path, default=REPORTS)
    args = parser.parse_args(argv)

    if args.mode == "real" and not args.yes:
        print("--mode real sends the fixtures' logs and the demo repository's code to your model provider. Add --yes to go ahead.", file=sys.stderr)
        return 2
    if not args.repo.exists():
        print(f"the demo shop repository is not at {args.repo} (clone log0labs/log0-demo-shop there, or pass --repo)", file=sys.stderr)
        return 2

    cases = load_cases()
    if args.cases:
        wanted = set(args.cases.split(","))
        unknown = wanted - {c.id for c in cases}
        if unknown:
            print(f"unknown case ids: {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.id in wanted]

    retrieval = asyncio.run(run_retrieval(load_retrieval_cases(), EVALS / "runbooks"))
    baseline_path = None if args.no_baseline else (args.baseline or (BASELINE if BASELINE.exists() else None))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path else None

    specs: list[str | None] = args.compare.split(",") if args.compare else [None]
    summaries, exit_code = [], 0
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for spec in specs:
        print(f"running {len(cases)} cases ({spec or args.mode}) ...")
        results, label = asyncio.run(run_all(cases, args, spec))
        summary = summarize(results, retrieval, label=label)
        summaries.append(summary)
        degraded = bool(summary["degraded"])
        note = "" if args.mode == "real" else "Scripted model: these numbers test the harness, they are not log0's quality."
        report = markdown(results, summary, baseline=baseline if len(specs) == 1 else None, threshold=args.threshold, note=note)
        slug = (spec or args.mode).replace(":", "-").replace("/", "-")
        (args.output / f"{stamp}-{slug}.md").write_text(report, encoding="utf-8")
        (args.output / f"{stamp}-{slug}.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        # decide and write everything that matters BEFORE printing: printing is what crashed the first real run
        if baseline is not None and len(specs) == 1 and regressions(baseline, summary, threshold=args.threshold):
            exit_code = 1
        elif degraded:
            exit_code = max(exit_code, 3)
        if args.update_baseline and len(specs) == 1:
            if degraded:
                emit(
                    f"NOT writing a baseline: {len(summary['degraded'])} case(s) had model failures ({', '.join(summary['degraded'])}). "
                    "Re-run when the provider is healthy.",
                    sys.stderr,
                )
            else:
                BASELINE.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
                emit(f"baseline written to {BASELINE}")
        emit(report)
    if len(summaries) > 1:
        table = comparison(summaries)
        (args.output / f"{stamp}-comparison.md").write_text(table, encoding="utf-8")
        emit(table)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
