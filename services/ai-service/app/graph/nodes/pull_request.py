"""From an approved diff to a draft pull request: the V2 half of the graph.

    validate_changes -> offer_pr -> approve_pr -> create_branch -> apply_patch -> run_tests -> open_draft_pr -> finalize
(a failed run of the repository's checks may loop once through prepare_repair, propose_changes, validate_changes and apply_repair)

``offer_pr`` decides, once, whether a pull request can be offered (log0 may write to the repository)
and stores the offer in the state. ``approve_pr`` is a human gate like ``human_review``: it parks the
run with ``interrupt()`` and waits for an engineer; because the offer is already in the state, resuming
re-runs nothing that could change its mind. The three write nodes then run only for an approved decision, each one idempotent: it first checks whether its result is
already in the state, and the publisher itself returns an existing branch, commit or pull request
instead of making a second one, so a run resumed after a crash continues where it stopped.

The model plays no part here. What is committed is rebuilt from the validated changes at the
pinned commit and must produce exactly the diff the engineer reviewed, otherwise nothing is written.
A failed write is a gap, never a failed run: the diff from V1 is still the result.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from app.diff.builder import build_checked_diff, file_modes
from app.diff.validator import validate_changes as validate
from app.graph.code_access import CodeAccessError
from app.graph.state import InvestigationContext, InvestigationState
from app.publishing.ci import all_completed, label_for, outcome_of
from app.publishing.naming import (
    branch_name,
    commit_message,
    pull_request_body,
    pull_request_title,
)
from app.publishing.policy import check_diff, check_rate
from app.publishing.port import PublishError, PublishFile, PublishTarget
from app.sandbox.port import SandboxRun
from app.schemas.domain import CheckResult, CiRun, PrDecision

logger = logging.getLogger(__name__)

GATE = "pr_approval"
_sleep = asyncio.sleep  # replaced in tests so waiting for CI takes no time
BASE_LABEL = "log0"
MAX_TEXT = 600


def pr_request(state: InvestigationState, target: PublishTarget) -> dict[str, Any]:
    """What the engineer is shown while the run waits: where, which branch, which files. Plain JSON, no code."""
    repo, cause, incident = state["repo"], state["root_cause"], state["incident"]
    assert repo is not None and cause is not None and incident is not None
    return {
        "gate": GATE,
        "repository": target.repo,
        "defaultBranch": target.default_branch,
        "branch": branch_name(UUID(state["incident_id"]), UUID(state["investigation_id"])),
        "baseSha": repo.sha,
        "rootCause": cause.statement[:MAX_TEXT],
        "confidenceBand": state["confidence_band"],
        "files": [
            {"path": f.file_path, "operation": f.operation, "additions": f.additions, "deletions": f.deletions}
            for f in state["diff_files"]
        ],
    }


def read_pr_decision(raw: Any) -> PrDecision | None:
    """The resume value as a decision, or ``None`` when it is not a usable one (an approval needs a user id)."""
    try:
        decision = PrDecision.model_validate(raw)
        if decision.action == "approve":
            UUID(str(decision.decided_by))
    except (ValidationError, ValueError):
        return None
    return decision


def _eligible(state: InvestigationState, ctx: InvestigationContext) -> bool:
    return bool(
        ctx.publisher
        and state["diff"]
        and not state["validation_errors"]
        and state["repo"]
        and state["incident"]
        and state["root_cause"]
    )


async def offer_pr(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Decide whether a draft pull request can be offered for the validated diff; store the offer if so."""
    ctx = runtime.context
    if not _eligible(state, ctx):
        return {"pr_offer": None}
    assert ctx.publisher is not None and state["repo"] is not None
    target = await ctx.publisher.target_for(tenant_id=ctx.tenant_id, repo=state["repo"].repo)
    if target is None:
        return {"pr_offer": None}  # writing is off for this repository: the diff is the result, as in V1
    reasons = check_diff(state["diff_files"], ctx.pr_limits)
    if not reasons:
        reasons = check_rate(await ctx.publisher.activity(tenant_id=ctx.tenant_id, target=target), ctx.pr_limits)
    if reasons:
        return {"pr_offer": None, "gaps": [f"No pull request was offered: {'; '.join(reasons)}. The diff is the fix."]}
    return {"pr_offer": pr_request(state, target)}


async def approve_pr(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Wait for the engineer's answer to the stored offer."""
    offer = state["pr_offer"]
    assert offer is not None
    raw = interrupt(offer)

    decision = read_pr_decision(raw)
    if decision is None:
        logger.warning("approve_pr: the resume value was not a valid decision, no pull request is opened")
        return {"pr_decision": PrDecision(action="decline"), "gaps": ["The pull request decision could not be read, so none was opened"]}
    return {"pr_decision": decision}


def _approver(state: InvestigationState) -> UUID | None:
    """The engineer who approved, or ``None`` when there is no approval that names one (nothing may be written)."""
    decision = state["pr_decision"]
    if decision is None or decision.action != "approve":
        return None
    try:
        return UUID(str(decision.decided_by))
    except ValueError:
        return None


async def _target(state: InvestigationState, ctx: InvestigationContext) -> PublishTarget:
    assert ctx.publisher is not None and state["repo"] is not None
    target = await ctx.publisher.target_for(tenant_id=ctx.tenant_id, repo=state["repo"].repo)
    if target is None:
        raise PublishError("writing to this repository is no longer allowed")
    return target


def _failed(error: PublishError) -> dict[str, Any]:
    return {"gaps": [f"No pull request was opened: {error}"]}


async def create_branch(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    ctx = runtime.context
    approver = _approver(state)
    if state["branch"] is not None or approver is None or ctx.publisher is None or state["repo"] is None:
        return {}
    try:
        target = await _target(state, ctx)
        branch = await ctx.publisher.create_branch(
            tenant_id=ctx.tenant_id,
            target=target,
            investigation_id=UUID(state["investigation_id"]),
            incident_id=UUID(state["incident_id"]),
            base_sha=state["repo"].sha,
            approved_by=approver,
        )
    except PublishError as exc:
        return _failed(exc)
    return {"branch": branch, "tool_calls": 1}


async def _files_to_commit(state: InvestigationState, ctx: InvestigationContext) -> list[PublishFile]:
    """The files of the commit, rebuilt from the validated changes at the pinned commit; refuses to proceed
    unless they produce exactly the diff the engineer reviewed."""
    repo, incident = state["repo"], state["incident"]
    assert ctx.code is not None and repo is not None and incident is not None
    if reasons := check_diff(state["diff_files"], ctx.pr_limits):
        raise PublishError(f"the change is not allowed in a pull request: {reasons[0]}")
    try:
        async with ctx.code.snapshot(tenant_id=ctx.tenant_id, service_name=incident.service_name, sha=repo.sha) as checkout:
            if checkout is None:
                raise PublishError("the repository is no longer connected")
            root = checkout.code.root
            report = await asyncio.to_thread(validate, root, state["proposed_changes"], read_paths=state["read_paths"])
            if not report.ok:
                raise PublishError("the change no longer validates against the reviewed commit")
            built = await build_checked_diff(report.edits, root, git_binary=checkout.git.git_binary)
            if not built.applies or built.diff != state["diff"]:
                raise PublishError("the change to publish differs from the reviewed diff, so nothing was written")
            existing = [e.file_path for e in report.edits if e.original is not None and e.updated is not None]
            modes = await asyncio.to_thread(file_modes, checkout.git.git_binary, root, existing)
    except CodeAccessError as exc:
        raise PublishError(str(exc)) from None
    return [PublishFile(e.file_path, e.updated, modes.get(e.file_path) == "100755") for e in report.edits]


async def apply_patch(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    ctx = runtime.context
    branch, cause = state["branch"], state["root_cause"]
    if state["commit_sha"] is not None or branch is None or cause is None or ctx.publisher is None or ctx.code is None:
        return {}
    try:
        files = await _files_to_commit(state, ctx)
        sha = await ctx.publisher.commit(
            tenant_id=ctx.tenant_id,
            target=await _target(state, ctx),
            investigation_id=UUID(state["investigation_id"]),
            branch=branch,
            files=files,
            message=commit_message(cause, UUID(state["investigation_id"])),
        )
    except PublishError as exc:
        return _failed(exc)
    return {"commit_sha": sha, "tool_calls": 2}


_SANDBOX_CONCLUSION = {"passed": "success", "failed": "failure", "timeout": "timed_out"}


def _apply_files(root: Path, files: list[PublishFile]) -> None:
    """Write the change into the private snapshot, refusing any path that leaves it."""
    base = root.resolve()
    for file in files:
        target = (base / file.path).resolve()
        if not target.is_relative_to(base):
            raise PublishError("a changed file lies outside the repository")
        if file.content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.content.encode("utf-8"))


def _ci_run_from(run: SandboxRun) -> CiRun | None:
    if run.status == "error":
        return None
    check = CheckResult(name=f"log0 sandbox: {run.name}", status="completed", conclusion=_SANDBOX_CONCLUSION[run.status])
    return CiRun(
        outcome=run.status,
        checks=[check],
        source="sandbox",
        detail=None if run.status == "passed" else run.output_tail,
    )


async def _run_in_sandbox(state: InvestigationState, ctx: InvestigationContext) -> tuple[CiRun | None, str | None]:
    """Run the repository's tests in log0's isolated sandbox on the reviewed change. ``(run, gap)``: a failed sandbox is a gap."""
    repo, incident = state["repo"], state["incident"]
    assert ctx.sandbox is not None and ctx.code is not None and repo is not None and incident is not None
    try:
        files = await _files_to_commit(state, ctx)  # the same rebuild-and-compare as the commit: it is the reviewed change
        async with ctx.code.snapshot(tenant_id=ctx.tenant_id, service_name=incident.service_name, sha=repo.sha) as checkout:
            if checkout is None:
                return None, "The tests could not be run in the sandbox: the repository is no longer connected"
            await asyncio.to_thread(_apply_files, checkout.code.root, files)
            result = await ctx.sandbox.run(source=checkout.code.root, subdir=repo.path_prefix)
    except (PublishError, CodeAccessError, ValueError, OSError) as exc:
        return None, f"The tests could not be run in the sandbox: {exc}"
    if result is None:
        return None, None  # a toolchain the sandbox does not know: the pull request says nothing verified the change
    run = _ci_run_from(result)
    if run is None:
        return None, f"The sandbox could not run the tests: {result.output_tail}"
    return run, None


async def run_tests(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Wait (bounded) for the repository's own CI on the commit log0 made, and keep what it said.

    log0 never runs customer code: the checks that run on the pushed branch are the repository's own.
    A repository without CI, or one that does not start a check in time, is ``none``; a check that is still
    running when the time is up is ``timeout``. Not being able to read the checks is a gap, not a failure.
    """
    ctx = runtime.context
    sha, cause = state["commit_sha"], state["root_cause"]
    if state["ci_run"] is not None or sha is None or cause is None or ctx.publisher is None:
        return {}
    wait = ctx.ci_wait
    poll = max(wait.poll_seconds, 0.01)
    waited = 0.0
    try:
        target = await _target(state, ctx)
        while True:
            checks = await ctx.publisher.check_results(tenant_id=ctx.tenant_id, target=target, commit_sha=sha)
            if all_completed(checks):
                outcome = outcome_of(checks)
                break
            if waited >= (wait.total_seconds if checks else wait.start_grace_seconds):
                outcome = "timeout" if checks else "none"
                break
            await _sleep(poll)
            waited += poll
    except PublishError as exc:
        return {"gaps": [f"The checks of the repository could not be read: {exc}"]}
    run = CiRun(outcome=outcome, checks=checks)
    extra_gaps: list[str] = []
    if run.outcome == "none" and ctx.sandbox is not None and ctx.sandbox_tests:
        sandboxed, problem = await _run_in_sandbox(state, ctx)
        if sandboxed is not None:
            run = sandboxed
        if problem:
            extra_gaps.append(problem)
    await ctx.publisher.record_ci_run(tenant_id=ctx.tenant_id, investigation_id=UUID(state["investigation_id"]), commit_sha=sha, run=run)
    return {"ci_run": run, **({"gaps": extra_gaps} if extra_gaps else {})}


def _restore_committed(state: InvestigationState) -> dict[str, Any]:
    """Put back the change that is already committed (a repair gave up or was refused): it is what the pull request holds."""
    return {
        "repairing": False,
        "ci_failure": None,
        "diff": state["committed_diff"],
        "diff_files": state["committed_files"],
        "proposed_changes": state["committed_changes"],
        "validation_errors": [],
    }


async def prepare_repair(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """The repository's checks failed on the commit: read what they printed and let the proposer try once more.

    Nothing is committed here. The change that is on the branch is remembered so that a repair that gives up leaves
    exactly that in place. Without a readable log (or with repairs switched off) log0 does not guess.
    """
    ctx = runtime.context
    attempts = state["repair_attempts"] + 1
    if ctx.publisher is None or state["ci_run"] is None or state["commit_sha"] is None or ctx.ci_repair_attempts < attempts:
        return {"repair_attempts": attempts}
    try:
        target = await _target(state, ctx)
        log = await ctx.publisher.ci_failure_log(tenant_id=ctx.tenant_id, target=target, checks=state["ci_run"].checks)
    except PublishError:
        log = ""
    if not log:
        return {"repair_attempts": attempts, "gaps": ["The checks failed, but log0 could not read their output, so the change was not corrected"]}
    return {
        "repair_attempts": attempts,
        "repairing": True,
        "ci_failure": log,
        "change_attempts": 0,
        "validation_errors": [],
        "previous_commit": state["commit_sha"],
        "committed_diff": state["diff"],
        "committed_files": state["diff_files"],
        "committed_changes": state["proposed_changes"],
    }


async def abandon_repair(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    return {**_restore_committed(state), "gaps": ["log0 could not produce a corrected change that validates, so the pull request keeps the first change"]}


async def apply_repair(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Commit the corrected change on top of the first commit, then wait for the checks again.

    A repair may never widen what the engineer approved: it can only touch files of the approved change. It must also
    be a different change from the one that failed. Anything else leaves the first change in place.
    """
    ctx = runtime.context
    cause, branch, previous = state["root_cause"], state["branch"], state["previous_commit"]
    if not state["repairing"] or cause is None or branch is None or previous is None or ctx.publisher is None or ctx.code is None:
        return {}
    approved = {f.file_path for f in state["committed_files"]}
    touched = {f.file_path for f in state["diff_files"]}
    if not touched <= approved:
        return {**_restore_committed(state), "gaps": ["The corrected change touches files the engineer did not approve, so it was not committed"]}
    if state["diff"] == state["committed_diff"]:
        return {**_restore_committed(state), "gaps": ["The corrected change is the same as the one that failed, so it was not committed again"]}
    try:
        files = await _files_to_commit(state, ctx)
        sha = await ctx.publisher.commit(
            tenant_id=ctx.tenant_id,
            target=await _target(state, ctx),
            investigation_id=UUID(state["investigation_id"]),
            branch=branch,
            files=files,
            message=commit_message(cause, UUID(state["investigation_id"])),
            parent_sha=previous,
        )
    except PublishError as exc:
        return {**_restore_committed(state), **_failed(exc)}
    return {"commit_sha": sha, "ci_run": None, "repairing": False, "ci_failure": None, "tool_calls": 2}


async def open_draft_pr(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    ctx = runtime.context
    branch, sha, cause = state["branch"], state["commit_sha"], state["root_cause"]
    if state["pull_request"] is not None or branch is None or sha is None or cause is None or ctx.publisher is None:
        return {}
    ci = state["ci_run"]
    if ctx.pr_only_on_green and ci is not None and ci.outcome in ("failed", "timeout"):
        return {
            "gaps": [
                f"No pull request was opened: the checks of the repository did not pass and this server only opens pull requests for passing commits (the change is on branch {branch.name})"
            ]
        }
    outcome_label = label_for(ci.outcome) if ci else None
    try:
        pull_request = await ctx.publisher.open_pull_request(
            tenant_id=ctx.tenant_id,
            target=await _target(state, ctx),
            investigation_id=UUID(state["investigation_id"]),
            branch=branch,
            commit_sha=sha,
            title=pull_request_title(cause),
            body=pull_request_body(
                root_cause=cause,
                confidence_band=state["confidence_band"],
                changes=state["proposed_changes"],
                files=state["diff_files"],
                ci_run=ci,
                repaired=state["repair_attempts"] > 0 and state["commit_sha"] != state["previous_commit"],
            ),
            labels=[BASE_LABEL, *([outcome_label] if outcome_label else [])],
        )
    except PublishError as exc:
        return _failed(exc)
    return {"pull_request": pull_request, "tool_calls": 1}
