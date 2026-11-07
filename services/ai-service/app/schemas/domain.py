"""Domain objects the investigation graph passes around.

These are internal: they live in the graph state and the database, not in the HTTP API,
so they use plain snake_case Pydantic models (no camelCase aliases).
"""

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["log", "timeline", "incident", "code", "commit", "runbook", "doc", "memory"]


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
    """What the cheap first LLM pass decides (finalised in step 2.8)."""

    category: Literal["dependency", "deployment", "resource", "code_bug", "config", "unknown"]
    summary: str
    gather: list[Literal["logs", "history", "code", "runbooks"]]  # which fan-out branches to run


class ReviewDecision(BaseModel):
    action: Literal["approve", "refine", "stop"]
    note: str | None = None  # the engineer's guidance when action == "refine"


class RepoContext(BaseModel):
    """Which repository and exact commit the code tools read (filled in M3)."""

    repo: str  # "owner/name"
    branch: str
    sha: str  # pinned for the whole investigation
    path_prefix: str = ""  # service directory inside a monorepo
