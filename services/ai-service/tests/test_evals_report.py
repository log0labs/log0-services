"""Step 6.5: the report, the baseline comparison (the regression gate) and the model comparison table."""

from evals.cases import Case, Expected, RetrievalCase
from evals.evaluators import Metric, Outcome
from evals.report import comparison, markdown, regressions, summarize
from evals.runner import CaseResult, RetrievalResult


def case(case_id: str) -> Case:
    return Case(id=case_id, title=case_id, fixture="f", service="payments", repo_ref="r", expected=Expected(categories=["code_bug"], reference="r"))


def result(case_id: str, scores: dict[str, float | None], *, seconds: float = 1.0, band: str | None = "high", cost: float | None = None) -> CaseResult:
    from app.schemas.domain import RootCause

    outcome = Outcome(seconds=seconds, tool_calls=3, input_tokens=100, output_tokens=20, cost_usd=cost, confidence_band=band, root_cause=RootCause(statement="s", evidence_ids=["e"]))
    return CaseResult(case(case_id), outcome, [Metric(n, s, f"detail of {n}") for n, s in scores.items()])


RETRIEVAL = [RetrievalResult(RetrievalCase(id="q", query="q", relevant=["a"]), ["a"], 1.0, 1.0), RetrievalResult(RetrievalCase(id="q2", query="q", relevant=["a"]), ["b"], 0.0, 0.0)]


def test_summary_averages_each_metric_over_the_cases_it_applies_to():
    results = [result("a", {"root_cause": 1.0, "fix_pattern": 1.0}), result("b", {"root_cause": 0.0, "fix_pattern": None})]
    s = summarize(results, RETRIEVAL, label="x")
    assert s["metrics"] == {"root_cause": 0.5, "fix_pattern": 1.0}
    assert s["cases"]["b"] == {"root_cause": 0.0, "fix_pattern": None}
    assert s["retrieval"] == {"queries": 2, "context_precision": 0.5, "context_recall": 0.5}


def test_summary_operations_numbers():
    s = summarize([result("a", {"root_cause": 1.0}, seconds=2, cost=0.01), result("b", {"root_cause": 1.0}, seconds=4, cost=0.03)], [], label="x")
    assert s["ops"]["cases"] == 2 and s["ops"]["latency_p50_s"] == 3.0 and s["ops"]["tool_calls_total"] == 6
    assert s["ops"]["input_tokens"] == 200 and s["ops"]["cost_usd"] == 0.04
    assert summarize([result("a", {"root_cause": 1.0})], [], label="x")["ops"]["cost_usd"] is None  # unpriced stays unpriced


def test_calibration_counts_a_root_cause_as_correct_only_at_full_marks():
    results = [result("a", {"root_cause": 1.0}, band="high"), result("b", {"root_cause": 0.5}, band="high"), result("c", {"root_cause": 0.0}, band="low")]
    s = summarize(results, [], label="x")
    assert s["calibration"] == [{"band": "high", "n": 2, "accuracy": 0.5}, {"band": "low", "n": 1, "accuracy": 0.0}]


BASELINE = {
    "label": "main",
    "metrics": {"root_cause": 0.8, "fix_pattern": 0.7},
    "retrieval": {"context_precision": 0.7, "context_recall": 0.8},
    "cases": {"a": {"root_cause": 1.0}, "b": {"root_cause": 0.5}},
}


def current(metrics, retrieval=None, cases=None):
    return {"metrics": metrics, "retrieval": retrieval or {"context_precision": 0.7, "context_recall": 0.8}, "cases": cases or {"a": {"root_cause": 1.0}, "b": {"root_cause": 0.5}}}


def test_no_regression_when_nothing_dropped_beyond_the_threshold():
    assert regressions(BASELINE, current({"root_cause": 0.78, "fix_pattern": 0.7}), threshold=0.05) == []
    assert regressions(BASELINE, current({"root_cause": 0.9, "fix_pattern": 0.9})) == []


def test_a_metric_that_drops_more_than_the_threshold_is_a_regression():
    found = regressions(BASELINE, current({"root_cause": 0.7, "fix_pattern": 0.7}), threshold=0.05)
    assert [str(r) for r in found] == ["metric root_cause: 0.80 -> 0.70"]


def test_retrieval_drops_count_too():
    found = regressions(BASELINE, current({"root_cause": 0.8, "fix_pattern": 0.7}, retrieval={"context_precision": 0.5, "context_recall": 0.8}))
    assert [r.name for r in found] == ["retrieval.context_precision"]


def test_a_case_that_goes_from_passing_to_failing_is_a_regression_even_if_the_average_holds():
    found = regressions(BASELINE, current({"root_cause": 0.8, "fix_pattern": 0.7}, cases={"a": {"root_cause": 0.0}, "b": {"root_cause": 0.5}}))
    assert [str(r) for r in found] == ["case a / root_cause: 1.00 -> 0.00"]


def test_metrics_or_cases_missing_from_one_side_are_not_regressions():
    assert regressions(BASELINE, current({"root_cause": 0.8}, cases={"a": {"root_cause": 1.0}})) == []
    assert regressions({"metrics": {"x": None}}, {"metrics": {"x": 0.1}}) == []
    assert regressions({}, {}) == []


def test_the_markdown_report_has_every_section():
    results = [result("a", {"root_cause": 1.0, "fix_pattern": 0.0}), result("b", {"root_cause": 0.5, "fix_pattern": None})]
    s = summarize(results, RETRIEVAL, label="scripted oracle")
    text = markdown(results, s, baseline=BASELINE, note="harness only")
    for part in ("# log0 evaluation: scripted oracle", "> harness only", "## Metrics", "| root_cause | 0.75 | 0.80 |", "## Cases", "| a | 1.00 | 0.00 |",
                 "| b | 0.50 | n/a |", "## Calibration", "## What went wrong", "**a** / fix_pattern: detail of fix_pattern", "## Against the baseline (main", "retrieval context precision"):
        assert part in text, part


def test_the_report_says_when_there_is_no_regression_and_lists_regressions_when_there_are():
    results = [result("a", {"root_cause": 1.0})]
    ok = markdown(results, summarize(results, [], label="x"), baseline={"label": "b", "metrics": {"root_cause": 1.0}, "cases": {}})
    assert "No regression." in ok
    bad = markdown(results, summarize(results, [], label="x"), baseline={"label": "b", "metrics": {"root_cause": 1.0}}, threshold=0.0)
    assert "No regression." in bad  # equal is not a drop
    worse = markdown([result("a", {"root_cause": 0.0})], summarize([result("a", {"root_cause": 0.0})], [], label="x"), baseline={"label": "b", "metrics": {"root_cause": 1.0}})
    assert "REGRESSION: metric root_cause: 1.00 -> 0.00" in worse


def test_the_model_comparison_table_has_a_row_per_model():
    a = summarize([result("a", {"root_cause": 1.0, "localization_hit@1": 1.0}, cost=0.02)], [], label="groq:x")
    b = summarize([result("a", {"root_cause": 0.0})], [], label="openai:y")
    table = comparison([a, b])
    lines = table.strip().splitlines()
    assert len(lines) == 4 and lines[2].startswith("| groq:x | 1.00 | 1.00 | n/a") and lines[3].startswith("| openai:y | 0.00 | n/a")
    assert "0.02" in lines[2] and "n/a |" in lines[3]
