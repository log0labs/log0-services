"""Gather branch: what does the code say? (the repository mapped to the incident's service)

This node connects the code investigator (3.7) to the investigation:

1. ask ``ctx.code`` for the service's checkout (3.1 mapping, 3.2 token, 3.4 clone) and pin the
   commit: ``state["repo"]`` records exactly which code was read, and the row stores its sha;
2. work out how much of the investigation's budget the code investigation may use;
3. run ``investigate_code`` with the incident brief and the parsed stack frames;
4. turn each *grounded* finding into citable ``Evidence``, with an excerpt read **from the
   repository** (not written by the model), so a later step can quote real code or a real commit.

Like every gather node it never fails the run. No repository mapped, a token that cannot be
read, a clone that fails, too little budget left, a model that is down: each is a gap that says what
was not examined, and the rest of the investigation continues without code evidence.
"""

import logging
import time
from typing import Any

from langgraph.runtime import Runtime

from app.graph.code_access import CodeAccessError, CodeCheckout
from app.graph.state import InvestigationContext, InvestigationState
from app.graph.subgraphs.code_investigator import (
    CodeInvestigatorContext,
    CodeLimits,
    investigate_code,
)
from app.graph.subgraphs.code_tools import build_toolset
from app.schemas.code_findings import CodeFinding
from app.schemas.domain import Evidence, make_evidence_id
from app.tools.code import CodeToolError
from app.tools.incidents import IncidentContext

logger = logging.getLogger(__name__)

_OPEN_CALLS = 1  # looking up the repository and making sure the checkout is current
MAX_CODE_CALLS = 16  # the most the code investigation may use (seeding included)
RESERVED_CALLS = 8  # kept for the other branches and for verify's follow-up searches
MIN_CODE_CALLS = 4  # below this the investigation cannot do anything useful
MAX_CODE_SECONDS = 90.0
RESERVED_SECONDS = 120.0  # kept for synthesize, verify and root_cause
MIN_CODE_SECONDS = 20.0
EXCERPT_LINES = 40
MAX_CONTENT = 4000
MAX_SUMMARY = 300
MAX_BRIEF_MESSAGES = 3
MAX_BRIEF_MESSAGE_CHARS = 200
MAX_UNRESOLVED_GAPS = 3


def plan_limits(state: InvestigationState, ctx: InvestigationContext) -> tuple[CodeLimits | None, str | None]:
    """How much the code investigation may spend, or ``(None, reason)`` when too little is left.

    The parent's budget is shared: calls already made are subtracted, and some are kept back for
    the steps that still have to run after this branch.
    """
    calls = min(MAX_CODE_CALLS, ctx.budget.max_tool_calls - state["tool_calls"] - _OPEN_CALLS - RESERVED_CALLS)
    seconds = min(MAX_CODE_SECONDS, ctx.budget.max_wall_seconds - (time.monotonic() - ctx.started_at) - RESERVED_SECONDS)
    if calls < MIN_CODE_CALLS:
        return None, "tool-call budget left was too small"
    if seconds < MIN_CODE_SECONDS:
        return None, "time budget left was too small"
    return CodeLimits(max_tool_calls=calls, max_seconds=seconds), None


def _stamp(value: Any) -> str:
    return value.strftime("%Y-%m-%dT%H:%MZ")


def build_brief(incident: IncidentContext, state: InvestigationState) -> str:
    """The incident in plain text for the investigator. Deliberately without the earlier AI summary:
    a previous model's guess would anchor the new analysis."""
    lines = [
        f"Service: {incident.service_name} ({incident.environment})",
        f"Severity {incident.severity}, {incident.occurrence_count} occurrences.",
        f"The incident began at {_stamp(incident.first_seen_at)} and was last seen at {_stamp(incident.last_seen_at)}.",
    ]
    messages = [m.strip()[:MAX_BRIEF_MESSAGE_CHARS] for m in incident.top_messages[:MAX_BRIEF_MESSAGES] if m.strip()]
    if messages:
        lines.append("Top error messages:")
        lines += [f"- {m}" for m in messages]
    triage = state["triage"]
    if triage is not None:
        lines.append(f"First assessment: {triage.summary}")
    if state["question"]:
        lines.append(f"The engineer asked: {state['question'][:300]}")
    return "\n".join(lines)


async def finding_evidence(finding: CodeFinding, checkout: CodeCheckout) -> tuple[Evidence | None, int]:
    """One finding as one piece of evidence, with an excerpt read from the repository.

    ``recent_change`` findings become ``commit`` evidence, the others ``code`` evidence; when the
    preferred kind has nothing to cite (a recent change with no commit) the other kind is used.
    Returns ``(evidence, tool calls made)``.
    """
    sha8 = checkout.repo.sha[:8]
    summary = " ".join(finding.summary.split())
    prefer_commit = (finding.kind == "recent_change" and bool(finding.commits)) or not finding.locations

    if prefer_commit and finding.commits:
        commit = finding.commits[0]
        ref = f"commit {commit[:8]}"
        content = ""
        try:
            detail = await checkout.git.show_commit(commit)
            files = ", ".join(f.path for f in detail.files[:10])
            content = f"{detail.short_sha} {detail.date:%Y-%m-%d} {detail.author}\n{detail.message}\nfiles: {files}\n\n{detail.patch}"[:MAX_CONTENT]
            who = f"{detail.author}, {detail.date:%Y-%m-%d}"
        except CodeToolError:
            who = "date unknown"
        return (
            Evidence(id=make_evidence_id("commit", ref), kind="commit", source_ref=ref, summary=f"Commit {commit[:8]} ({who}): {summary}"[:MAX_SUMMARY], content=content),
            1,
        )

    if finding.locations:
        location = finding.locations[0]
        ref = f"{location.path}:{location.start_line}-{location.end_line}@{sha8}"
        content = ""
        try:
            piece = await checkout.code.read_file(location.path, location.start_line, min(location.end_line, location.start_line + EXCERPT_LINES - 1))
            content = piece.text[:MAX_CONTENT]
        except CodeToolError:
            pass  # the finding is still grounded (the path was seen); only the excerpt is missing
        return (
            Evidence(id=make_evidence_id("code", ref), kind="code", source_ref=ref, summary=f"{location.path}:{location.start_line}-{location.end_line}: {summary}"[:MAX_SUMMARY], content=content),
            1,
        )
    return None, 0


async def gather_code(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Add code and commit evidence, pin ``repo``, count the tool calls. Never fails the run."""
    incident = state["incident"]
    if incident is None:
        return {}
    ctx = runtime.context

    if ctx.code is None:
        return {"gaps": ["The code was not searched: code investigation is not available (this server has no repository access configured)"]}
    if ctx.models is None:
        raise RuntimeError("InvestigationContext.models is not set: gather_code needs a chat model")

    try:
        checkout = await ctx.code.open(tenant_id=ctx.tenant_id, service_name=incident.service_name)
    except CodeAccessError as exc:
        return {"gaps": [f"The code was not examined: {exc}"], "tool_calls": _OPEN_CALLS}
    except Exception:
        logger.warning("opening the code failed", exc_info=True)
        return {"gaps": ["The code was not examined: the repository could not be opened"], "tool_calls": _OPEN_CALLS}
    if checkout is None:
        return {
            "gaps": [f"The code was not examined: no repository is connected for the service {incident.service_name!r} (connect one in the AI settings)"],
            "tool_calls": _OPEN_CALLS,
        }

    limits, reason = plan_limits(state, ctx)
    if limits is None:
        return {"repo": checkout.repo, "gaps": [f"The code was not examined because the {reason}"], "tool_calls": _OPEN_CALLS}

    investigator = CodeInvestigatorContext(tools=build_toolset(checkout.code, checkout.git), models=ctx.models, limits=limits)
    # No run config is passed: LangChain carries the parent's callbacks into a graph invoked inside a node,
    # so the subgraph's model calls and node timings land in the same usage recorder as the rest of the run
    # (a test checks it), and nothing else of the parent's config (checkpointing included) is inherited.
    result = await investigate_code(investigator, brief=build_brief(incident, state), frames=state["stack_frames"])

    evidence: list[Evidence] = []
    extra_calls = 0
    for finding in result.findings.findings:
        item, calls = await finding_evidence(finding, checkout)
        extra_calls += calls
        if item is not None:
            evidence.append(item)

    gaps = list(result.gaps)
    gaps += [f"Code investigation left open: {q}" for q in result.findings.unresolved[:MAX_UNRESOLVED_GAPS]]
    if not evidence and not gaps:
        gaps.append("The code was examined but nothing in it was found relevant to this incident")
    return {
        "repo": checkout.repo,
        "evidence": evidence,
        "gaps": gaps,
        "tool_calls": _OPEN_CALLS + result.findings.tool_calls + extra_calls,
    }
