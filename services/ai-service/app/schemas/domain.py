"""Domain objects the investigation graph passes around.

These are internal: they live in the graph state and the database, not in the HTTP API,
so they use plain snake_case Pydantic models (no camelCase aliases).
"""

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["log", "timeline", "incident", "code", "commit", "runbook", "doc", "memory"]
GatherBranch = Literal["logs", "history", "code", "runbooks"]


def make_evidence_id(kind: str, source_ref: str) -> str:
    """Stable id derived from what the evidence *is*: ``ev_`` + 8 hex chars.

    The same fact found twice (by two parallel nodes, or by a retried node) gets the same
    id, so the evidence reducer can deduplicate it. Sequential ids (ev_1, ev_2, ...) would
    collide when parallel nodes number their own evidence.
    """
    digest = hashlib.sha1(f"{kind}\0{source_ref}".encode()).hexdigest()
    return f"ev_{digest[:8]}"


class Evidence(BaseModel):
    id: str  # make_evidence_id(kind, source_ref): assigned by the tool layer, never by the LLM
    kind: EvidenceKind
    source_ref: str  # "payment/app.py:142@a81f2c", "commit a81f2c", "incident c9d8e7f6", ...
    summary: str  # one line, what the model reads first
    content: str = Field(default="", max_length=4000)  # bounded excerpt (log lines, code, diff)


class Hypothesis(BaseModel):
    id: str
    statement: str
    supporting_evidence: list[str] = Field(default_factory=list)  # evidence ids
    contradicting_evidence: list[str] = Field(default_factory=list)  # evidence ids
    status: Literal["open", "supported", "refuted"] = "open"


class CodeLocation(BaseModel):
    file_path: str
    line: int | None = None
    function: str | None = None


class RootCause(BaseModel):
    statement: str
    evidence_ids: list[str]  # must all exist: the deterministic grounding check
    affected_locations: list[CodeLocation] = Field(default_factory=list)
    contributing_factors: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    model_confidence: float = 0.0  # logged for calibration analysis only, never shown


class CodeChange(BaseModel):
    file_path: str
    operation: Literal["modify", "add", "delete"]
    explanation: str
    old_code: str | None = None  # must match the file at repo_sha exactly once
    new_code: str | None = None


class Triage(BaseModel):
    """What the cheap first LLM pass decides. It is also the schema the model must fill in,
    so every field carries a description the model reads."""

    category: Literal["dependency", "deployment", "resource", "code_bug", "config", "unknown"] = Field(
        description=(
            "Most likely kind of problem: dependency (a downstream service or database), "
            "deployment (a recent release or rollout), resource (memory, CPU, disk, "
            "connections, rate limits), code_bug (a defect in this service's code), "
            "config (settings or secrets), unknown (cannot tell from the facts)"
        )
    )
    summary: str = Field(description="One or two plain sentences: what seems to be happening")
    gather: list[GatherBranch] = Field(
        description=(
            "Which evidence sources are worth searching: logs (log timeline and samples), "
            "history (earlier incidents with the same fingerprint), code (the repository: "
            "needs an application stack frame or a clear code symptom), runbooks (team docs)"
        )
    )
    initial_hypotheses: list[str] = Field(
        default_factory=list,
        description="Up to three short, competing explanations to test; each one sentence",
    )


class HypothesisDraft(BaseModel):
    """One explanation as the model writes it. ``normalize_hypotheses`` turns it into a
    ``Hypothesis`` with a stable id, after checking that every cited id really exists."""

    statement: str = Field(
        description="One or two sentences: a specific explanation of why this incident happened, "
        "concrete enough that more evidence could confirm or refute it"
    )
    supporting_evidence: list[str] = Field(
        description="Ids of the evidence that supports it, copied exactly as given (like ev_1a2b3c4d). "
        "At least one."
    )
    contradicting_evidence: list[str] = Field(
        default_factory=list,
        description="Ids of evidence that argues against it, copied exactly as given. Empty if none.",
    )


class Synthesis(BaseModel):
    """What the synthesize step asks the model for."""

    hypotheses: list[HypothesisDraft] = Field(
        description="Two to four competing explanations, most likely first. Fewer when the "
        "evidence is thin: never invent one to fill the list."
    )


class HypothesisVerdict(BaseModel):
    """The model's judgement of one hypothesis. ``apply_verdicts`` checks it before it counts."""

    hypothesis_id: str = Field(description="The id of the hypothesis being judged, copied exactly (like hyp_1a2b3c4d)")
    verdict: Literal["supported", "refuted", "open"] = Field(
        description=(
            "supported: the evidence directly shows this explanation is true. "
            "refuted: the evidence directly shows it is false. "
            "open: the evidence does not settle it (the safe answer when unsure)"
        )
    )
    supporting_evidence: list[str] = Field(
        default_factory=list,
        description="Ids of evidence that supports it, copied exactly. Required for supported.",
    )
    contradicting_evidence: list[str] = Field(
        default_factory=list,
        description="Ids of evidence that argues against it, copied exactly. Required for refuted.",
    )
    follow_up_search: str | None = Field(
        default=None,
        description=(
            "Only for open: a short, distinctive piece of text (an exception name, an error "
            "phrase) that would appear in the log messages if this hypothesis were true. "
            "Null when no search would help."
        ),
    )


class Verification(BaseModel):
    """What the verify step asks the model for."""

    verdicts: list[HypothesisVerdict] = Field(description="One verdict for each open hypothesis")


class ReviewDecision(BaseModel):
    action: Literal["approve", "refine", "stop"]
    note: str | None = None  # the engineer's guidance when action == "refine"


class RepoContext(BaseModel):
    """Which repository and exact commit the code tools read (filled in M3)."""

    repo: str  # "owner/name"
    branch: str
    sha: str  # pinned for the whole investigation
    path_prefix: str = ""  # service directory inside a monorepo
