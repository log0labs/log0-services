"""LangGraph state for an investigation, the reducers that merge parallel writes, and the
runtime context that carries the tenant and backends.

State is what the graph *knows* and nodes update. Context is what the graph *runs with*
and nodes can only read. ``tenant_id`` lives in context, not state, so no node can
change it, and it never lands in a checkpoint a user could resume with another tenant.
"""

import operator
import time
from dataclasses import dataclass, field
from typing import Annotated, Protocol, TypedDict
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.graph.budget import Budget
from app.llm.purpose import Purpose, StructuredOutputMethod
from app.parsing.stacktrace import StackFrame
from app.schemas.domain import (
    CodeChange,
    CodeLocation,
    Evidence,
    Hypothesis,
    RepoContext,
    ReviewDecision,
    RootCause,
    Triage,
)
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.tools.incidents import IncidentApi, IncidentContext, RelatedIncidentSource
from app.tools.logs import LogBackend

# ------------------------------------------------------------------------ reducers
# A reducer decides what happens when a node returns a value for a key that already has
# one. Without a reducer LangGraph *replaces* the value, and if two parallel nodes write
# the same key in one step it raises InvalidUpdateError. These make parallel gathering safe.


class HasId(Protocol):
    id: str


def add_evidence(left: list[Evidence] | None, right: list[Evidence] | None) -> list[Evidence]:
    """Append new evidence; an id already present is kept as it was (first one wins)."""
    merged = list(left or [])
    seen = {e.id for e in merged}
    for item in right or []:
        if item.id not in seen:
            merged.append(item)
            seen.add(item.id)
    return merged


def merge_by_id[T: HasId](left: list[T] | None, right: list[T] | None) -> list[T]:
    """Update items that share an id (the new version wins, keeping its position); append new ones."""
    merged = {item.id: item for item in left or []}
    for item in right or []:
        merged[item.id] = item
    return list(merged.values())


def append_unique(left: list[str] | None, right: list[str] | None) -> list[str]:
    """Append strings not already present, so a retried node cannot duplicate a gap."""
    merged = list(left or [])
    for item in right or []:
        if item not in merged:
            merged.append(item)
    return merged


# --------------------------------------------------------------------------- state


class InvestigationState(TypedDict):
    # identity (tenant_id is deliberately NOT here: it is in InvestigationContext)
    investigation_id: str
    incident_id: str
    question: str | None
    scope: InvestigationScope
    
    # deterministic context, filled by load_context
    incident: IncidentContext | None
    stack_frames: list[StackFrame]  # parsed, not LLM-generated
    repo: RepoContext | None

    triage: Triage | None

    # evidence and reasoning: written by parallel nodes, so these have reducers
    evidence: Annotated[list[Evidence], add_evidence]
    hypotheses: Annotated[list[Hypothesis], merge_by_id]
    verify_round: int
    follow_ups: list[str]  # log searches verify wants run next; empty: no loop

    root_cause: RootCause | None
    review: ReviewDecision | None
    proposed_changes: list[CodeChange]
    validation_errors: list[str]
    change_attempts: int
    diff: str | None
    
    # budget and bookkeeping
    tool_calls: Annotated[int, operator.add]  # every tool call adds 1: the budget
    gaps: Annotated[list[str], append_unique]  # what we could not find out (shown to the user)
    status: InvestigationStatus
    error: str | None


def new_state(
    *,
    investigation_id: str,
    incident_id: str,
    scope: InvestigationScope,
    question: str | None = None,
) -> InvestigationState:
    """The initial state: every key present, so nodes and reducers never see a missing one."""
    return InvestigationState(
        investigation_id=investigation_id,
        incident_id=incident_id,
        question=question,
        scope=scope,
        incident=None,
        stack_frames=[],
        repo=None,
        triage=None,
        evidence=[],
        hypotheses=[],
        verify_round=0,
        follow_ups=[],
        root_cause=None,
        review=None,
        proposed_changes=[],
        validation_errors=[],
        change_attempts=0,
        diff=None,
        tool_calls=0,
        gaps=[],
        status=InvestigationStatus.RUNNING,
        error=None,
    )


# ------------------------------------------------------------------------- context


class ChatModels(Protocol):
    """Hands out the chat model for a purpose. ``PurposeChatModels`` in production, a fake in tests."""

    def for_purpose(self, purpose: Purpose) -> BaseChatModel: ...
    
    def structured_method(self, purpose: Purpose) -> StructuredOutputMethod: ...


@dataclass(frozen=True)
class InvestigationContext:
    """Read-only, per-run. Nodes get it as ``runtime.context``."""

    tenant_id: UUID  # from the verified JWT, never from the request body or the model
    logs: LogBackend  # LogStore in production, a fixture in evals
    incidents: IncidentApi
    history: RelatedIncidentSource
    models: ChatModels | None = None  # needed by every LLM node; None only in tests of non-LLM nodes
    budget: Budget = field(default_factory=Budget)
    # When this run started (monotonic seconds). A run resumed after a restart gets a new context,
    # so its clock starts again: the limit is per process run, not across restarts.
    started_at: float = field(default_factory=time.monotonic)


# ------------------------------------------------------------------------ checkpoints
# A checkpoint stores the state as bytes; reading it back rebuilds our Pydantic models.
# LangGraph warns that it will refuse to rebuild types that were not registered, which
# would break resuming an investigation after a restart. Register every type that can
# appear in the state; the PostgresSaver is built with this serializer.

CHECKPOINT_TYPES: tuple[type, ...] = (
    InvestigationScope,
    InvestigationStatus,
    IncidentContext,
    StackFrame,
    RepoContext,
    Triage,
    Evidence,
    Hypothesis,
    RootCause,
    CodeLocation,
    ReviewDecision,
    CodeChange,
)


def checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=[(t.__module__, t.__name__) for t in CHECKPOINT_TYPES]
    )
