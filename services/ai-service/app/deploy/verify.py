"""The verdict after a deploy: is the error better, worse or unchanged, and is there enough traffic to say? Pure functions.

No model, no clock, no database: counts go in, a ``Judgement`` comes out, with the numbers it compared. The rules are the
ones of ADR-021:

* too little traffic is never "healthy" (silence can mean fixed or dead), and not "degraded" either: it cannot be judged;
* a new error above the threshold, or a known one that grew, is **degraded** and names the fingerprint;
* the investigated error at or above the baseline plus the margin is **degraded**; at or below the baseline minus the margin,
  and only when the soak is over, **healthy**; in between, **inconclusive** (unchanged);
* while the soak runs (``final=False``) only a clear degradation, after a minimum of observation, ends it early; everything
  else means "keep watching" (``verdict`` is ``None``).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.deploy.target import Thresholds
from app.schemas.remediation import Baseline, Verdict

MIN_EARLY_MINUTES = 5.0  # a degradation needs this much observation to end the soak before its time
GROWTH_FACTOR = 2.0  # a known error that is now above the threshold counts as growing when it doubled


@dataclass(frozen=True)
class Observation:
    """What the logs say about the window since the deploy (after the grace period)."""

    minutes: float
    errors: int  # occurrences of the investigated fingerprint
    traffic: int  # all log lines of the service
    others: Mapping[str, int] = field(default_factory=dict)  # other fingerprint -> occurrences


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict | None  # None: not decided yet, keep watching
    reasons: tuple[str, ...]
    numbers: dict[str, Any]


def _rate(count: int, minutes: float) -> float:
    return count / minutes if minutes > 0 else 0.0


def _growing(baseline: Baseline, observation: Observation, thresholds: Thresholds) -> list[tuple[str, float]]:
    """Other errors now above the threshold that were not there before, or have at least doubled. Worst first."""
    found = []
    for fingerprint, count in observation.others.items():
        now = _rate(count, observation.minutes)
        before = baseline.other_fingerprints.get(fingerprint, 0.0)
        if now > thresholds.new_error_per_minute and now > GROWTH_FACTOR * before:
            found.append((fingerprint, now))
    return sorted(found, key=lambda item: -item[1])


def judge(baseline: Baseline, observation: Observation, thresholds: Thresholds, *, final: bool) -> Judgement:
    """Decide, or say ``verdict=None`` while it is too early. ``final`` is true once the soak window is over."""
    rate = _rate(observation.errors, observation.minutes)
    before = baseline.errors_per_minute
    numbers: dict[str, Any] = {
        "baselineErrorsPerMinute": round(before, 3),
        "errorsPerMinute": round(rate, 3),
        "minutes": round(observation.minutes, 1),
        "traffic": observation.traffic,
        "minTraffic": thresholds.min_traffic,
        "margin": thresholds.improvement_margin,
    }
    early_ok = observation.minutes >= MIN_EARLY_MINUTES

    if observation.traffic < thresholds.min_traffic:
        if not final:
            return Judgement(None, (), numbers)
        return Judgement("inconclusive", (f"only {observation.traffic} log lines since the deploy (at least {thresholds.min_traffic} are needed to judge)",), numbers)

    growing = _growing(baseline, observation, thresholds)
    if growing and (final or early_ok):
        fingerprint, now = growing[0]
        numbers["newError"] = {"fingerprint": fingerprint, "perMinute": round(now, 3)}
        return Judgement("degraded", (f"another error ({fingerprint[:12]}) is now {now:.1f} per minute, above {thresholds.new_error_per_minute:g}",), numbers)

    margin = thresholds.improvement_margin
    if before <= 0:
        if rate > thresholds.new_error_per_minute and (final or early_ok):
            return Judgement("degraded", (f"the error was not happening before and is now {rate:.1f} per minute",), numbers)
        if final:
            return Judgement("inconclusive", ("the error was not happening before the deploy, so an improvement cannot be shown",), numbers)
        return Judgement(None, (), numbers)

    if rate >= before * (1 + margin) and (final or early_ok):
        return Judgement("degraded", (f"the error is {rate:.1f} per minute, worse than {before:.1f} before the deploy",), numbers)
    if not final:
        return Judgement(None, (), numbers)
    if rate <= before * (1 - margin):
        return Judgement("healthy", (f"the error fell from {before:.1f} to {rate:.1f} per minute",), numbers)
    return Judgement("inconclusive", (f"the error is {rate:.1f} per minute against {before:.1f} before: not clearly better or worse",), numbers)
