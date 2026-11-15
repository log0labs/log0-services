"""The verdict after a deploy, on synthetic counts: fixed, unchanged, worse, a new error, no traffic, too early. No I/O."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.deploy.target import Thresholds
from app.deploy.verify import MIN_EARLY_MINUTES, Observation, judge
from app.schemas.remediation import Baseline

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
TH = Thresholds()  # margin 0.5, min traffic 100, new error 5 per minute


def _baseline(rate: float = 4.0, others: dict[str, float] | None = None) -> Baseline:
    return Baseline(window_start=T0 - timedelta(minutes=30), window_end=T0, errors_per_minute=rate, total_logs=6000, other_fingerprints=others or {})


def _obs(errors: int, minutes: float = 15.0, traffic: int = 3000, others: dict[str, int] | None = None) -> Observation:
    return Observation(minutes=minutes, errors=errors, traffic=traffic, others=others or {})


def test_an_error_that_fell_below_the_margin_when_the_soak_is_over_is_healthy() -> None:
    j = judge(_baseline(4.0), _obs(errors=15), TH, final=True)  # 1 per minute against 4: below 2

    assert j.verdict == "healthy" and "fell from 4.0 to 1.0" in j.reasons[0]
    assert j.numbers["baselineErrorsPerMinute"] == 4.0 and j.numbers["errorsPerMinute"] == 1.0 and j.numbers["traffic"] == 3000


def test_an_error_that_is_gone_is_healthy() -> None:
    assert judge(_baseline(4.0), _obs(errors=0), TH, final=True).verdict == "healthy"


def test_healthy_is_never_declared_before_the_soak_is_over() -> None:
    j = judge(_baseline(4.0), _obs(errors=0, minutes=14), TH, final=False)

    assert j.verdict is None  # keep watching: a quiet first minutes prove nothing


def test_an_error_about_as_frequent_as_before_is_inconclusive() -> None:
    j = judge(_baseline(4.0), _obs(errors=60), TH, final=True)  # 4 per minute

    assert j.verdict == "inconclusive" and "not clearly better or worse" in j.reasons[0]


@pytest.mark.parametrize(("errors", "verdict"), [(30, "healthy"), (31, "inconclusive"), (89, "inconclusive"), (90, "degraded"), (200, "degraded")])
def test_the_margin_decides_at_its_edges(errors: int, verdict: str) -> None:
    # baseline 4 per minute, margin 0.5: healthy up to 2 per minute (30 in 15 minutes), degraded from 6 per minute (90)
    assert judge(_baseline(4.0), _obs(errors=errors), TH, final=True).verdict == verdict


def test_an_error_that_got_worse_ends_the_soak_early_once_there_is_enough_observation() -> None:
    soon = judge(_baseline(4.0), _obs(errors=100, minutes=MIN_EARLY_MINUTES - 1), TH, final=False)
    later = judge(_baseline(4.0), _obs(errors=100, minutes=MIN_EARLY_MINUTES), TH, final=False)

    assert soon.verdict is None  # two bad minutes could be a restart
    assert later.verdict == "degraded" and "worse than 4.0" in later.reasons[0]


def test_an_error_that_is_merely_unchanged_does_not_end_the_soak_early() -> None:
    assert judge(_baseline(4.0), _obs(errors=40, minutes=10), TH, final=False).verdict is None


def test_a_new_error_above_the_threshold_is_degraded_and_names_the_fingerprint() -> None:
    j = judge(_baseline(4.0), _obs(errors=0, others={"e" * 64: 150}), TH, final=True)  # 10 per minute, never seen before

    assert j.verdict == "degraded" and "eeeeeeeeeeee" in j.reasons[0]
    assert j.numbers["newError"] == {"fingerprint": "e" * 64, "perMinute": 10.0}


def test_a_new_error_below_the_threshold_is_noise() -> None:
    j = judge(_baseline(4.0), _obs(errors=0, others={"e" * 64: 30}), TH, final=True)  # 2 per minute

    assert j.verdict == "healthy"


def test_a_known_error_is_degraded_only_if_it_also_at_least_doubled() -> None:
    known = _baseline(4.0, {"e" * 64: 4.0})

    steady = judge(known, _obs(errors=0, others={"e" * 64: 90}), TH, final=True)  # 6 per minute: above 5 but not double 4
    doubled = judge(known, _obs(errors=0, others={"e" * 64: 135}), TH, final=True)  # 9 per minute: above 8

    assert steady.verdict == "healthy" and doubled.verdict == "degraded"


def test_the_worst_new_error_is_the_one_named() -> None:
    j = judge(_baseline(4.0), _obs(errors=0, others={"a" * 64: 100, "b" * 64: 300}), TH, final=True)

    assert j.numbers["newError"]["fingerprint"] == "b" * 64


def test_a_new_error_early_needs_the_minimum_observation_too() -> None:
    assert judge(_baseline(4.0), _obs(errors=0, minutes=2, others={"e" * 64: 100}), TH, final=False).verdict is None
    assert judge(_baseline(4.0), _obs(errors=0, minutes=6, others={"e" * 64: 100}), TH, final=False).verdict == "degraded"


def test_too_little_traffic_is_inconclusive_at_the_end_and_silent_before() -> None:
    quiet = _obs(errors=0, traffic=TH.min_traffic - 1)

    final = judge(_baseline(4.0), quiet, TH, final=True)
    during = judge(_baseline(4.0), quiet, TH, final=False)

    assert final.verdict == "inconclusive" and "at least 100 are needed" in final.reasons[0]
    assert during.verdict is None


def test_silence_with_no_traffic_is_never_healthy() -> None:
    assert judge(_baseline(4.0), _obs(errors=0, traffic=0), TH, final=True).verdict != "healthy"


def test_even_a_new_error_is_not_judged_without_traffic() -> None:
    assert judge(_baseline(4.0), _obs(errors=0, traffic=10, others={"e" * 64: 500}), TH, final=True).verdict == "inconclusive"


@pytest.mark.parametrize(("errors", "verdict"), [(0, "inconclusive"), (30, "inconclusive"), (90, "degraded")])
def test_when_the_error_was_not_happening_before_an_improvement_cannot_be_shown(errors: int, verdict: str) -> None:
    j = judge(_baseline(0.0), _obs(errors=errors), TH, final=True)  # 0, 2 and 6 per minute against a baseline of 0

    assert j.verdict == verdict


def test_a_zero_baseline_waits_while_the_soak_runs() -> None:
    assert judge(_baseline(0.0), _obs(errors=0, minutes=10), TH, final=False).verdict is None


def test_thresholds_are_the_tenants_to_tune() -> None:
    strict = Thresholds.model_validate({"improvementMargin": 0.9})  # healthy only below a tenth of the baseline

    assert judge(_baseline(4.0), _obs(errors=15), strict, final=True).verdict == "inconclusive"
    assert judge(_baseline(4.0), _obs(errors=3), strict, final=True).verdict == "healthy"


def test_zero_minutes_does_not_divide_by_zero() -> None:
    j: Any = judge(_baseline(4.0), _obs(errors=5, minutes=0), TH, final=False)

    assert j.verdict is None and j.numbers["errorsPerMinute"] == 0.0
