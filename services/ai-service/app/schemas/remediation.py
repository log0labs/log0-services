"""Vocabulary of remediation (V3): deploy a merged fix, verify it with log0's own signals, roll back if it is worse.

Plain values and pure functions, no database and no network. The status table and the transition rules are the
code form of the status diagram in ``v3-graph-diagram.md``; the database check constraints are tested against these
constants, so the two cannot drift apart.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, Field

RemediationStatus = Literal[
    "OFFERED",
    "PREFLIGHT",
    "AWAITING_DEPLOY_APPROVAL",
    "DEPLOYING",
    "VERIFYING",
    "AWAITING_DECISION",
    "AWAITING_ROLLBACK_APPROVAL",
    "ROLLING_BACK",
    "VERIFIED",
    "ROLLED_BACK",
    "DECLINED",
    "ABANDONED",
    "FAILED",
    "CANCELLED",
]
DeployKind = Literal["deploy", "rollback"]
DeploymentStatus = Literal["dispatched", "running", "succeeded", "failed", "cancelled", "timeout"]
Verdict = Literal["healthy", "degraded", "inconclusive"]
Gate = Literal["deploy", "decision", "rollback"]
Decision = Literal["approve", "decline", "keep", "extend", "rollback"]
ReleaseSource = Literal["log0", "github", "manual"]

STATUSES: tuple[str, ...] = get_args(RemediationStatus)
KINDS: tuple[str, ...] = get_args(DeployKind)
DEPLOYMENT_STATUSES: tuple[str, ...] = get_args(DeploymentStatus)
VERDICTS: tuple[str, ...] = get_args(Verdict)
GATES: tuple[str, ...] = get_args(Gate)
RELEASE_SOURCES: tuple[str, ...] = get_args(ReleaseSource)

TRANSITIONS: Mapping[str, frozenset[str]] = {
    "OFFERED": frozenset({"PREFLIGHT", "ABANDONED", "CANCELLED"}),
    "PREFLIGHT": frozenset({"AWAITING_DEPLOY_APPROVAL", "OFFERED", "FAILED", "CANCELLED"}),
    "AWAITING_DEPLOY_APPROVAL": frozenset({"DEPLOYING", "DECLINED", "CANCELLED"}),
    "DEPLOYING": frozenset({"VERIFYING", "FAILED", "CANCELLED"}),
    "VERIFYING": frozenset({"VERIFIED", "AWAITING_DECISION", "AWAITING_ROLLBACK_APPROVAL", "ROLLING_BACK", "FAILED", "CANCELLED"}),
    "AWAITING_DECISION": frozenset({"VERIFYING", "VERIFIED", "AWAITING_ROLLBACK_APPROVAL", "CANCELLED"}),
    "AWAITING_ROLLBACK_APPROVAL": frozenset({"ROLLING_BACK", "VERIFIED", "CANCELLED"}),
    "ROLLING_BACK": frozenset({"ROLLED_BACK", "FAILED", "CANCELLED"}),
    "VERIFIED": frozenset(),
    "ROLLED_BACK": frozenset(),
    "DECLINED": frozenset(),
    "ABANDONED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}

TERMINAL_STATUSES: frozenset[str] = frozenset(status for status, targets in TRANSITIONS.items() if not targets)
"""Nothing leaves these states."""

ACTIVE_STATUSES: frozenset[str] = frozenset(STATUSES) - TERMINAL_STATUSES - {"OFFERED"}
"""Statuses that hold the environment: at most one remediation per repository and environment is in one of them."""

WAITING_FOR_A_HUMAN: frozenset[str] = frozenset({"AWAITING_DEPLOY_APPROVAL", "AWAITING_DECISION", "AWAITING_ROLLBACK_APPROVAL"})


def can_transition(current: str, new: str) -> bool:
    """Is moving from ``current`` to ``new`` allowed by the status diagram? Unknown statuses are never allowed."""
    return new in TRANSITIONS.get(current, frozenset())


class PolicyDecision(BaseModel):
    """The outcome of the deterministic policy check, with every reason shown to the person who has to decide."""

    allowed: bool
    reasons: list[str] = Field(default_factory=list)


class Baseline(BaseModel):
    """What the logs looked like in the window before the deploy; verification compares the soak window to this."""

    window_start: datetime
    window_end: datetime
    errors_per_minute: float = Field(ge=0)
    total_logs: int = Field(ge=0)
    other_fingerprints: dict[str, float] = Field(default_factory=dict)  # fingerprint -> errors per minute


class Approval(BaseModel):
    """A human's answer at one of the gates, recorded with who, in which role and about which commit."""

    gate: Gate
    decision: Decision
    user_id: str
    role: str
    environment: str
    sha: str
    decided_at: datetime
