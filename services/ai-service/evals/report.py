"""Turn case results into a Markdown report and a JSON summary, compare with a baseline, compare models.

The JSON summary is what is committed as a baseline and diffed on the next run:

    {"version": 1, "label": "...", "metrics": {"root_cause": 0.81, ...},
     "cases": {"bug-04-nil-map": {"root_cause": 1.0, ...}}, "ops": {...}}

A run *regresses* when a metric's average drops by more than the threshold, or a single case's score goes from
passing (at least 0.5) to failing (0). Both are reported; either makes ``evals.run`` exit non-zero.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from evals.evaluators import METRIC_ORDER, average, calibration_rows
from evals.runner import CaseResult, RetrievalResult

VERSION = 1


def _fmt(score: float | None) -> str:
    return "n/a" if score is None else f"{score:.2f}"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summarize(results: list[CaseResult], retrieval: list[RetrievalResult], *, label: str) -> dict[str, Any]:
    names = [n for n in METRIC_ORDER if any(r.score(n) is not None for r in results)]
    metrics = {n: average([m for r in results for m in r.metrics if m.name == n]) for n in names}
    seconds = [r.outcome.seconds for r in results]
    costs = [r.outcome.cost_usd for r in results if r.outcome.cost_usd is not None]
    correct = [(r.outcome.confidence_band, (r.score("root_cause") or 0) >= 1.0) for r in results if r.outcome.root_cause]
    summary: dict[str, Any] = {
        "version": VERSION,
        "label": label,
        "metrics": {n: (round(v, 4) if v is not None else None) for n, v in metrics.items()},
        "cases": {r.case.id: {m.name: m.score for m in r.metrics} for r in results},
        "calibration": [{"band": b, "n": n, "accuracy": round(a, 4)} for b, n, a in calibration_rows(correct)],
        "ops": {
            "cases": len(results),
            "latency_p50_s": round(statistics.median(seconds), 2) if seconds else 0.0,
            "latency_p95_s": round(_percentile(seconds, 0.95), 2),
            "tool_calls_total": sum(r.outcome.tool_calls for r in results),
            "input_tokens": sum(r.outcome.input_tokens for r in results),
            "output_tokens": sum(r.outcome.output_tokens for r in results),
            "cost_usd": round(sum(costs), 4) if costs else None,
        },
        "retrieval": {
            "queries": len(retrieval),
            "context_precision": round(statistics.mean(r.precision for r in retrieval), 4) if retrieval else None,
            "context_recall": round(statistics.mean(r.recall for r in retrieval), 4) if retrieval else None,
        },
    }
    return summary


@dataclass(frozen=True)
class Regression:
    kind: str  # "metric" | "case"
    name: str
    before: float
    after: float

    def __str__(self) -> str:
        return f"{self.kind} {self.name}: {self.before:.2f} -> {self.after:.2f}"


def regressions(baseline: dict[str, Any], current: dict[str, Any], *, threshold: float = 0.05) -> list[Regression]:
    found: list[Regression] = []
    for name, before in baseline.get("metrics", {}).items():
        after = current.get("metrics", {}).get(name)
        if before is not None and after is not None and before - after > threshold:
            found.append(Regression("metric", name, before, after))
    retrieval = {"context_precision": None, "context_recall": None}
    for name in retrieval:
        before, after = baseline.get("retrieval", {}).get(name), current.get("retrieval", {}).get(name)
        if before is not None and after is not None and before - after > threshold:
            found.append(Regression("metric", f"retrieval.{name}", before, after))
    for case, scores in baseline.get("cases", {}).items():
        now = current.get("cases", {}).get(case)
        if now is None:
            continue
        for name, before in scores.items():
            after = now.get(name)
            if before is not None and after is not None and before >= 0.5 and after == 0:
                found.append(Regression("case", f"{case} / {name}", before, after))
    return found


def markdown(results: list[CaseResult], summary: dict[str, Any], *, baseline: dict[str, Any] | None = None, threshold: float = 0.05, note: str = "") -> str:
    lines = [f"# log0 evaluation: {summary['label']}", ""]
    if note:
        lines += [f"> {note}", ""]
    ops = summary["ops"]
    cost = ops["cost_usd"] if ops["cost_usd"] is not None else "not priced"
    lines += [
        (
            f"{ops['cases']} cases, {ops['tool_calls_total']} tool calls, p50 {ops['latency_p50_s']} s, p95 {ops['latency_p95_s']} s, "
            f"{ops['input_tokens']} input / {ops['output_tokens']} output tokens, cost {cost} USD."
        ),
        "",
    ]

    lines += ["## Metrics", "", "| Metric | Score |" + (" Baseline |" if baseline else ""), "|---|---|" + ("---|" if baseline else "")]
    for name, value in summary["metrics"].items():
        row = f"| {name} | {_fmt(value)} |"
        if baseline:
            row += f" {_fmt(baseline.get('metrics', {}).get(name))} |"
        lines.append(row)
    if summary["retrieval"]["queries"]:
        r = summary["retrieval"]
        lines += [f"| retrieval context precision ({r['queries']} queries) | {_fmt(r['context_precision'])} |" + (" |" if baseline else ""),
                  f"| retrieval context recall | {_fmt(r['context_recall'])} |" + (" |" if baseline else "")]

    names = list(summary["metrics"])
    lines += ["", "## Cases", "", "| Case | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for r in results:
        lines.append(f"| {r.case.id} | " + " | ".join(_fmt(r.score(n)) for n in names) + " |")

    if summary["calibration"]:
        lines += ["", "## Calibration (confidence band against a correct root cause)", "", "| Band | Cases | Correct |", "|---|---|---|"]
        lines += [f"| {c['band']} | {c['n']} | {c['accuracy']:.2f} |" for c in summary["calibration"]]

    failing = [(r.case.id, m) for r in results for m in r.metrics if m.score is not None and m.score < 1.0]
    if failing:
        lines += ["", "## What went wrong", ""]
        lines += [f"- **{case}** / {m.name}: {m.detail}" for case, m in failing]

    if baseline is not None:
        found = regressions(baseline, summary, threshold=threshold)
        lines += ["", f"## Against the baseline ({baseline.get('label', 'unnamed')}, threshold {threshold})", ""]
        lines += [f"- REGRESSION: {r}" for r in found] if found else ["No regression."]
    return "\n".join(lines) + "\n"


def comparison(summaries: list[dict[str, Any]]) -> str:
    """The model-selection table: the same dataset across several models."""
    key = ["root_cause", "localization_hit@1", "fix_pattern", "diff_valid", "no_secret_leak"]
    lines = ["| Model | " + " | ".join(key) + " | p50 s | tool calls | tokens (in/out) | cost USD |", "|---|" + "---|" * (len(key) + 4)]
    for s in summaries:
        ops = s["ops"]
        lines.append(
            f"| {s['label']} | " + " | ".join(_fmt(s["metrics"].get(k)) for k in key)
            + f" | {ops['latency_p50_s']} | {ops['tool_calls_total']} | {ops['input_tokens']}/{ops['output_tokens']} | {ops['cost_usd'] if ops['cost_usd'] is not None else 'n/a'} |"
        )
    return "\n".join(lines) + "\n"
