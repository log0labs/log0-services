"""Reading the repository's own CI: when is a set of checks decided, and what did it decide.

Pure functions plus a small policy value. Waiting itself (polling GitHub) is the graph node's job.
"""

from dataclasses import dataclass

from app.schemas.domain import CheckResult

_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"})
_PASSED = frozenset({"success", "neutral", "skipped"})


@dataclass(frozen=True)
class CiWait:
    total_seconds: float = 600.0
    poll_seconds: float = 15.0
    start_grace_seconds: float = 60.0


def all_completed(checks: list[CheckResult]) -> bool:
    return bool(checks) and all(c.status == "completed" for c in checks)


def outcome_of(checks: list[CheckResult]) -> str:
    """``passed`` or ``failed`` for completed checks. One failing check fails the commit; an unknown
    conclusion is not trusted as green."""
    if any(c.conclusion in _FAILED for c in checks):
        return "failed"
    return "passed" if all(c.conclusion in _PASSED for c in checks) else "failed"


def label_for(outcome: str) -> str | None:
    """The pull request label for an outcome (``none`` and ``timeout`` have none: nothing was decided)."""
    return {"passed": "log0:tests-passing", "failed": "log0:tests-failing"}.get(outcome)
