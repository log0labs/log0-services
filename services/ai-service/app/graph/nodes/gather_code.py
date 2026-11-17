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
import re
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any

from langgraph.runtime import Runtime

from app.graph.code_access import CodeAccessError, CodeCheckout
from app.graph.nodes.load_context import investigation_window
from app.graph.state import InvestigationContext, InvestigationState
from app.graph.subgraphs.code_investigator import (
    CodeInvestigationResult,
    CodeInvestigatorContext,
    CodeLimits,
    investigate_code,
)
from app.graph.subgraphs.code_tools import build_toolset
from app.integrations.clickhouse import ClickHouseError
from app.schemas.code_findings import CodeFinding
from app.schemas.domain import Evidence, make_evidence_id
from app.tools.code import CodeToolError
from app.tools.incidents import IncidentContext
from app.tools.logs import get_cooccurring_errors

logger = logging.getLogger(__name__)

_OPEN_CALLS = 1  # looking up the repository and making sure the checkout is current
MAX_CODE_CALLS = 32  # the most the code investigation may use (seeding included)
RESERVED_CALLS = 8  # kept for the other branches and for verify's follow-up searches
FOLLOWUP_CALLS = 16  # the most the second pass, which answers what the first left open, may use
FOLLOWUP_SECONDS = 120.0
MIN_CODE_CALLS = 4  # below this the investigation cannot do anything useful
MAX_CODE_SECONDS = 180.0
RESERVED_SECONDS = 120.0  # kept for synthesize, verify and root_cause
MIN_CODE_SECONDS = 20.0
EXCERPT_LINES = 40
MAX_CONTENT = 4000
MAX_SUMMARY = 300
MAX_BRIEF_MESSAGES = 3
MAX_BRIEF_MESSAGE_CHARS = 200
MAX_UNRESOLVED_GAPS = 3
MAX_LOG_SITES = 3  # other services' new log messages that are looked up in the code
MIN_ANCHOR_CHARS = 12  # shorter fixed text matches too much code to point anywhere
ONSET_SLACK = timedelta(minutes=5)
MAX_COMMIT_FILES = 4  # files of the suspect commit that become code evidence
MAX_COMMIT_RANGES = 8  # pieces of those files that are read
PIECE_LINES = 60  # the most lines of one piece
MERGE_GAP = 10  # hunks closer than this many lines are one piece
_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(?P<path>\S+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_PLACEHOLDER = re.compile(r"<[a-z_]+>")  # the fingerprinter writes <uuid>, <number>, ... for the variable parts of a message


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


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return "/test/" in f"/{path.lower()}" or "/tests/" in f"/{path.lower()}" or ".test." in name or "_test." in name or name.startswith("test_")


def changed_ranges(patch: str, limit: int = MAX_COMMIT_FILES) -> list[tuple[str, int, int]]:
    """The lines a commit's patch changed, as ``(path, first line, last line)`` of the new file, one range per changed place.

    Hunks that are close together are merged; far apart ones stay separate, so a change at the top and one at the bottom of
    a long file do not turn into one range whose middle is never read. A range longer than ``PIECE_LINES`` is cut into pieces,
    and at most ``MAX_COMMIT_RANGES`` come back. Deleted files, test files and anything past ``limit`` files are left out:
    the code a fix must change is the code the suspect commit changed, not the tests that cover it.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    for line in patch.splitlines():
        header = _DIFF_FILE.match(line)
        if header:
            path = header["path"]
            continue
        hunk = _HUNK.match(line)
        if hunk is None or path is None or _is_test_path(path):
            continue
        start = int(hunk["start"])
        if start == 0:  # the file no longer exists
            continue
        end = start + max(int(hunk["count"] or 1), 1) - 1
        places = ranges.setdefault(path, [])
        if places and start - places[-1][1] <= MERGE_GAP:
            places[-1] = (places[-1][0], max(places[-1][1], end))
        else:
            places.append((start, end))
    pieces = [
        (name, first, min(first + PIECE_LINES - 1, last))
        for name, places in list(ranges.items())[:limit]
        for begin, last in places
        for first in range(begin, last + 1, PIECE_LINES)
    ]
    return pieces[:MAX_COMMIT_RANGES]


async def commit_code_evidence(commit: str, checkout: CodeCheckout) -> tuple[list[Evidence], int]:
    """The code the suspect commit changed, as citable evidence with the lines read from the repository: ``(evidence, tool calls)``.

    A model that finds the right commit but cites no file leaves the fix proposal with nothing to work on. The commit names
    the files, so they are added here, whatever the model wrote.
    """
    sha8 = checkout.repo.sha[:8]
    try:
        detail = await checkout.git.show_commit(commit)
    except CodeToolError:
        return [], 1
    first_line = (detail.message.strip().splitlines() or [""])[0][:100]
    found: list[Evidence] = []
    calls = 1
    for path, first, last in changed_ranges(detail.patch):
        calls += 1
        try:
            piece = await checkout.code.read_file(path, first, last)
        except CodeToolError:
            continue
        ref = f"{path}:{first}-{last}@{sha8}"
        summary = f"{path}:{first}-{last}: changed by commit {detail.short_sha} ({first_line})"[:MAX_SUMMARY]
        found.append(Evidence(id=make_evidence_id("code", ref), kind="code", source_ref=ref, summary=summary, content=piece.text[:MAX_CONTENT]))
    return found, calls


def literal_anchor(template: str) -> str | None:
    """The longest run of fixed text in a log message template, or ``None`` when there is too little to search for.

    ``hold expired for order <uuid>: stock returned`` gives ``stock returned`` (or the longer run), which the code that writes
    that log line contains word for word.
    """
    runs = [part.strip(" :,.-\"'()[]") for part in _PLACEHOLDER.split(template)]
    best = max(runs, key=len, default="")
    return best if len(best) >= MIN_ANCHOR_CHARS else None


async def other_service_log_sites(incident: IncidentContext, ctx: InvestigationContext, checkout: CodeCheckout) -> tuple[list[str], int]:
    """Where in the code the other services' failures that began with the incident are logged: ``(lines for the brief, tool calls)``.

    A failure often starts in one service and shows in another, and the log line of the first names no file. The text of the
    message is in the code that wrote it, so searching for it points the investigator at that service's code without asking
    a model to guess. Every step that cannot be done (no logs, nothing found) just yields fewer lines.
    """
    start, end = investigation_window(incident)
    try:
        neighbours = await get_cooccurring_errors(
            ctx.logs,
            tenant_id=ctx.tenant_id,
            start=start,
            end=end,
            exclude_fingerprint=incident.fingerprint,
            limit=10,
            onset=max(start, incident.first_seen_at - ONSET_SLACK),
        )
    except (ClickHouseError, ValueError):
        return [], 1
    began = [e for e in neighbours if e.service_name != incident.service_name and e.first_seen >= incident.first_seen_at - ONSET_SLACK][:MAX_LOG_SITES]
    wide = checkout.widened()
    lines: list[str] = []
    calls = 1
    for error in began:
        anchor = literal_anchor(error.message_template)
        if anchor is None:
            continue
        calls += 1
        try:
            found = await wide.code.search_code(anchor, fixed_strings=True, limit=2, context=0)
        except CodeToolError:
            continue
        if found.hits:
            hit = found.hits[0]
            lines.append(f'{error.service_name} logged "{error.message_template[:100]}" ({error.count}x) from {hit.path}:{hit.first_line}')
    return lines, calls


async def answer_open_questions(
    first: CodeInvestigationResult,
    limits: CodeLimits,
    investigator: CodeInvestigatorContext,
    incident: IncidentContext,
    state: InvestigationState,
    checkout: CodeCheckout,
    ctx: InvestigationContext,
    log_sites: tuple[str, ...],
) -> CodeInvestigationResult | None:
    """A second, shorter pass that answers what the first one wrote down as unresolved, or ``None`` when nothing was left open
    or too little budget remains. The result merges both passes: the findings of both, the questions of the second only.

    A question a model leaves open is usually one it could settle by reading the function it calls. The pass is told exactly
    which questions to answer, so it reads that code instead of stopping at the call site.
    """
    if not first.findings.unresolved:
        return None
    calls = min(FOLLOWUP_CALLS, limits.max_tool_calls - first.findings.tool_calls)
    seconds = min(FOLLOWUP_SECONDS, ctx.budget.max_wall_seconds - (time.monotonic() - ctx.started_at) - RESERVED_SECONDS)
    if calls < MIN_CODE_CALLS or seconds < MIN_CODE_SECONDS:
        return None
    questions = "\n".join(f"- {q}" for q in first.findings.unresolved)
    brief = (
        f"{build_brief(incident, state, checkout.siblings, log_sites)}\n\n"
        "A first pass left these questions open. Answer each by reading the code that decides it: open the body of every "
        "function the question concerns (in any service), not only the line that calls it:\n"
        f"{questions}"
    )
    second = await investigate_code(replace(investigator, limits=replace(limits, max_tool_calls=calls, max_seconds=seconds)), brief=brief, frames=[])
    known = {f.summary for f in first.findings.findings}
    merged = [*first.findings.findings, *[f for f in second.findings.findings if f.summary not in known]]
    return CodeInvestigationResult(
        findings=second.findings.model_copy(update={"findings": merged, "tool_calls": first.findings.tool_calls + second.findings.tool_calls}),
        gaps=[*first.gaps, *second.gaps],
    )


def build_brief(incident: IncidentContext, state: InvestigationState, siblings: tuple[str, ...] = (), log_sites: tuple[str, ...] = ()) -> str:
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
    if siblings:
        lines.append(f"The repository also holds other services: {', '.join(siblings)}. A failure here can start in one of them.")
    if log_sites:
        lines.append("Other services failed in the same minutes; the code that logs it:")
        lines += [f"- {site}" for site in log_sites]
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

    log_sites, site_calls = await other_service_log_sites(incident, ctx, checkout) if checkout.siblings else ([], 0)
    wide = checkout.widened()
    investigator = CodeInvestigatorContext(tools=build_toolset(wide.code, wide.git), models=ctx.models, limits=limits)
    # No run config is passed: LangChain carries the parent's callbacks into a graph invoked inside a node,
    # so the subgraph's model calls and node timings land in the same usage recorder as the rest of the run
    # (a test checks it), and nothing else of the parent's config (checkpointing included) is inherited.
    result = await investigate_code(investigator, brief=build_brief(incident, state, checkout.siblings, tuple(log_sites)), frames=state["stack_frames"])

    followup = await answer_open_questions(result, limits, investigator, incident, state, checkout, ctx, tuple(log_sites))
    if followup is not None:
        result = followup

    evidence: list[Evidence] = []
    extra_calls = 0
    expanded = False
    for finding in result.findings.findings:
        item, calls = await finding_evidence(finding, checkout)
        extra_calls += calls
        if item is not None:
            evidence.append(item)
        if item is not None and item.kind == "commit" and not expanded and finding.commits:
            expanded = True  # one suspect commit is opened up into the code it changed
            changed, more = await commit_code_evidence(finding.commits[0], checkout)
            extra_calls += more
            known = {e.id for e in evidence}
            evidence += [e for e in changed if e.id not in known]

    gaps = list(result.gaps)
    gaps += [f"Code investigation left open: {q}" for q in result.findings.unresolved[:MAX_UNRESOLVED_GAPS]]
    if not evidence and not gaps:
        gaps.append("The code was examined but nothing in it was found relevant to this incident")
    return {
        "repo": checkout.repo,
        "evidence": evidence,
        "gaps": gaps,
        "tool_calls": _OPEN_CALLS + result.findings.tool_calls + extra_calls + site_calls,
    }
