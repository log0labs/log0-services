"""The nodes of the remediation graph, as far as M13 builds it:

    preflight -> capture_baseline -> approve_deploy -> trigger_deploy -> wait_deploy (-> park -> wait_deploy ...)

There is no model anywhere in this graph. ``preflight`` and ``capture_baseline`` only read and decide; a refusal leaves
the offer standing (it can be started again when the reason is gone). ``approve_deploy`` is a human gate. The two
nodes that act (``trigger_deploy``, ``wait_deploy``) are idempotent: the deployment row, with its correlation id, is the
memory of what was already asked of the pipeline, so a run resumed after a crash adopts the run that exists instead of
asking for a second one. ``wait_deploy`` looks once and returns; waiting is ``park``'s job (a stored wake-up time the
sweeper honours), so nothing sleeps inside a request and a restart loses nothing.
"""

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from app.deploy.policy import DeployFacts, check_start, paths_in_diff, switches
from app.deploy.provider import DispatchRequest
from app.deploy.releases import deployed_sha
from app.deploy.state import RemediationContext, RemediationState
from app.deploy.target import EnvironmentRule
from app.integrations.clickhouse import ClickHouseError
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import DEPLOY_PERMISSIONS, missing_permissions
from app.integrations.github_writer import WriteRejectedError, WriteTarget
from app.integrations.incident_service import IncidentNotFoundError
from app.publishing.ci import all_completed, outcome_of
from app.schemas.domain import CheckResult
from app.schemas.remediation import Baseline, PolicyDecision
from app.store import deploy_audit, deploy_targets, deployments, releases
from app.store import events as event_store
from app.store import github_installations as installations
from app.store import investigations as investigation_store
from app.store import remediations as remediation_store
from app.store import repositories as repository_store
from app.tools.incidents import get_incident
from app.tools.logs import (
    get_cooccurring_errors,
    get_error_timeline,
    get_service_traffic,
)

logger = logging.getLogger(__name__)

GATE = "deploy_approval"
WAIT_GATE = "wait"
BASELINE_MINUTES = 30
DISPATCH_GRACE = timedelta(seconds=120)  # how long GitHub may take to show the run we asked for
OTHER_FINGERPRINTS_KEPT = 20

Ctx = Runtime[RemediationContext]


# --------------------------------------------------------------------------- helpers


def _ids(state: RemediationState) -> tuple[UUID, UUID]:
    return UUID(state["remediation_id"]), UUID(state["investigation_id"])


def _rule(state: RemediationState) -> EnvironmentRule:
    return EnvironmentRule.model_validate(state["rule"])


async def _emit(ctx: RemediationContext, state: RemediationState, type: str, **payload: Any) -> None:
    _, investigation_id = _ids(state)
    await event_store.append_event_safely(
        ctx.sessions,
        investigation_id=investigation_id,
        type=type,
        payload={"remediationId": state["remediation_id"], "environment": state["environment"], **payload},
    )


async def _audit(ctx: RemediationContext, state: RemediationState, action: str, result: str, *, detail: str = "", actor: UUID | None = None) -> None:
    remediation_id, _ = _ids(state)
    async with ctx.sessions() as session:
        await deploy_audit.append(
            session,
            tenant_id=ctx.tenant_id,
            remediation_id=remediation_id,
            action=action,
            result=result,
            actor_id=actor,
            environment=state["environment"],
            sha=state["merge_sha"],
            detail=detail,
        )


def _write_target(repo: repository_store.RepositoryConnection | None, installation: installations.Installation | None) -> WriteTarget | None:
    if repo is None or installation is None or installation.suspended or not repo.installation_id or not repo.github_repository_id:
        return None
    return WriteTarget(repo.owner, repo.name, repo.installation_id, repo.github_repository_id, repo.default_branch)


def _ci_outcome(checks: list[CheckResult]) -> str:
    if not checks:
        return "none"
    return outcome_of(checks) if all_completed(checks) else "pending"


async def _refuse(ctx: RemediationContext, state: RemediationState, reasons: list[str], *, code: str = "deploy.refused") -> dict[str, Any]:
    """Leave the offer standing with the reasons, so it can be started again once they are gone."""
    remediation_id, _ = _ids(state)
    decision = PolicyDecision(allowed=False, reasons=reasons)
    async with ctx.sessions() as session:
        await remediation_store.transition(
            session,
            tenant_id=ctx.tenant_id,
            remediation_id=remediation_id,
            from_status="PREFLIGHT",
            to="OFFERED",
            policy=decision.model_dump(mode="json"),
            outcome={"refused": reasons},
        )
    await _audit(ctx, state, code, "refused", detail="; ".join(reasons))
    await _emit(ctx, state, "remediation.refused", reasons=reasons)
    return {"outcome": "refused"}


# ------------------------------------------------------------------------- preflight


async def preflight(state: RemediationState, runtime: Ctx) -> dict[str, Any]:
    """May this deploy start? Gathers the facts (switches, the target's rule, the change's files, the checks of the
    merge commit, what the environment runs now, recent deploys) and asks the policy."""
    ctx = runtime.context
    remediation_id, investigation_id = _ids(state)
    environment = state["environment"]
    now = ctx.now()
    async with ctx.sessions() as session:
        row = await remediation_store.get(session, tenant_id=ctx.tenant_id, remediation_id=remediation_id)
        assert row is not None
        repo = await repository_store.get(session, tenant_id=ctx.tenant_id, repository_id=row.repository_id)
        investigation = await investigation_store.get_by_id(session, tenant_id=ctx.tenant_id, investigation_id=investigation_id)
        installation = await installations.get(session, tenant_id=ctx.tenant_id, installation_id=repo.installation_id) if repo and repo.installation_id else None
        try:
            target = await deploy_targets.get(session, tenant_id=ctx.tenant_id, repository_id=row.repository_id)
        except ValidationError:
            target = None
        recent = await remediation_store.deploys_since(
            session, tenant_id=ctx.tenant_id, repository_id=row.repository_id, environment=environment, since=now - timedelta(hours=1)
        )
        rule = target.config.rule_for(environment) if target else None
        previous = target.config.previous_of(environment) if target else None
        previous_verified = bool(
            previous and await remediation_store.verified(session, tenant_id=ctx.tenant_id, pull_request_id=row.pull_request_id, environment=previous.name)
        )

    problems: list[str] = []
    write_target = _write_target(repo, installation)
    ci_outcome: str | None = None
    current: str | None = None
    if write_target is None:
        problems.append("the repository is not reachable through an installation of the log0 GitHub App")
    else:
        problems += await _github_problems(ctx, write_target.installation_id)
        try:
            ci_outcome = _ci_outcome(await ctx.checks.check_runs(write_target, row.merge_sha, DEPLOY_PERMISSIONS))
        except (GitHubAccessError, GitHubUnavailableError):
            problems.append("the checks of the merge commit could not be read")
        try:
            async with ctx.sessions() as session:
                current = await deployed_sha(session, ctx.deploys, write_target, tenant_id=ctx.tenant_id, repository_id=row.repository_id, environment=environment)
        except (GitHubAccessError, GitHubUnavailableError):
            problems.append(f"could not tell what '{environment}' runs now, so a rollback target is not known")

    server_enabled, tenant_enabled = switches(ctx.settings, ctx.tenant_id)
    decision = check_start(
        DeployFacts(
            environment=environment,
            rule=rule,
            server_enabled=server_enabled,
            tenant_enabled=tenant_enabled,
            changed_paths=paths_in_diff(investigation.diff if investigation else None),
            now=now,
            deploys_last_hour=recent,
            max_per_hour=ctx.settings.deploy_max_per_environment_per_hour,
            ci_outcome=ci_outcome,
            previous_environment=previous.name if previous else None,
            previous_verified=previous_verified,
            already_deployed=current is not None and current == row.merge_sha,
        )
    )
    reasons = [*decision.reasons, *problems]
    if reasons or rule is None:
        return await _refuse(ctx, state, reasons)

    async with ctx.sessions() as session:
        await remediation_store.set_fields(
            session,
            tenant_id=ctx.tenant_id,
            remediation_id=remediation_id,
            policy=PolicyDecision(allowed=True).model_dump(mode="json"),
            rollback_target=current,
        )
    await _emit(ctx, state, "remediation.preflight_passed", rollbackTarget=current)
    gaps = [] if current else ["No earlier release of this environment is known, so a rollback will not be offered"]
    return {"rule": rule.model_dump(mode="json"), "rollback_target": current, "gaps": [*state.get("gaps", []), *gaps]}


async def _github_problems(ctx: RemediationContext, installation_id: int) -> list[str]:
    try:
        info = await ctx.installations.get_installation(installation_id)
    except (GitHubAccessError, GitHubUnavailableError):
        return ["could not check what the GitHub App may do"]
    missing = missing_permissions(info.permissions)
    if missing:
        return [f"the GitHub App has not been granted: {', '.join(sorted(missing))}"]
    return []


# --------------------------------------------------------------------- the baseline


async def capture_baseline(state: RemediationState, runtime: Ctx) -> dict[str, Any]:
    """What the logs look like right now, before the deploy: the fingerprint's rate, the other errors, the traffic.

    Verification compares the window after the deploy to this. Without a baseline nothing can be verified, so a
    log store or incident service that cannot be read refuses the deploy instead of continuing blind.
    """
    ctx = runtime.context
    remediation_id, investigation_id = _ids(state)
    async with ctx.sessions() as session:
        investigation = await investigation_store.get_by_id(session, tenant_id=ctx.tenant_id, investigation_id=investigation_id)
    assert investigation is not None
    end = ctx.now()
    start = end - timedelta(minutes=BASELINE_MINUTES)
    try:
        incident = await get_incident(ctx.incidents, tenant_id=ctx.tenant_id, incident_id=investigation.incident_id)
        timeline = await get_error_timeline(ctx.logs, tenant_id=ctx.tenant_id, fingerprint=incident.fingerprint, start=start, end=end, bucket="1m")
        traffic = await get_service_traffic(ctx.logs, tenant_id=ctx.tenant_id, service_name=incident.service_name, start=start, end=end)
        others = await get_cooccurring_errors(ctx.logs, tenant_id=ctx.tenant_id, start=start, end=end, exclude_fingerprint=incident.fingerprint, limit=OTHER_FINGERPRINTS_KEPT)
    except (ClickHouseError, IncidentNotFoundError, httpx.HTTPError):
        return await _refuse(ctx, state, ["the logs could not be read to capture a baseline, so the deploy could not be verified"])
    baseline = Baseline(
        window_start=start,
        window_end=end,
        errors_per_minute=timeline.total / BASELINE_MINUTES,
        total_logs=traffic,
        other_fingerprints={e.fingerprint: e.count / BASELINE_MINUTES for e in others},
    )
    dumped = baseline.model_dump(mode="json")
    async with ctx.sessions() as session:
        await remediation_store.set_fields(session, tenant_id=ctx.tenant_id, remediation_id=remediation_id, baseline=dumped)
    await _emit(ctx, state, "remediation.baseline_captured", errorsPerMinute=round(baseline.errors_per_minute, 3), totalLogs=traffic, minutes=BASELINE_MINUTES)
    return {"baseline": dumped}


# ---------------------------------------------------------------------- the human gate


def deploy_request(state: RemediationState) -> dict[str, Any]:
    """What the approver is shown while the run waits. Plain JSON: the commit, the environment, the numbers, the rules."""
    rule = _rule(state)
    baseline = state.get("baseline") or {}
    return {
        "gate": GATE,
        "environment": state["environment"],
        "sha": state["merge_sha"],
        "rollbackTarget": state.get("rollback_target"),
        "approver": rule.approver,
        "soakSeconds": rule.soak_seconds,
        "baseline": {"errorsPerMinute": baseline.get("errors_per_minute"), "totalLogs": baseline.get("total_logs"), "minutes": BASELINE_MINUTES},
    }


async def approve_deploy(state: RemediationState, runtime: Ctx) -> dict[str, Any]:
    """Park until a human answers. Nothing happens before ``interrupt()``, so resuming repeats nothing."""
    raw = interrupt(deploy_request(state))
    action = raw.get("action") if isinstance(raw, dict) else None
    if action not in ("approve", "decline"):
        logger.warning("approve_deploy: the resume value was not a decision, nothing is deployed")
        return {"approval": {"action": "decline"}, "outcome": "declined", "gaps": [*state.get("gaps", []), "The deploy decision could not be read, so nothing was deployed"]}
    approval = {"action": action, "decided_by": raw.get("decided_by"), "role": raw.get("role")}
    return {"approval": approval, "outcome": "declined" if action == "decline" else None}


# ---------------------------------------------------------------------------- the deploy


async def _target_and_config(ctx: RemediationContext, state: RemediationState) -> tuple[WriteTarget, str]:
    remediation_id, _ = _ids(state)
    async with ctx.sessions() as session:
        row = await remediation_store.get(session, tenant_id=ctx.tenant_id, remediation_id=remediation_id)
        assert row is not None
        repo = await repository_store.get(session, tenant_id=ctx.tenant_id, repository_id=row.repository_id)
        installation = await installations.get(session, tenant_id=ctx.tenant_id, installation_id=repo.installation_id) if repo and repo.installation_id else None
        target = await deploy_targets.get(session, tenant_id=ctx.tenant_id, repository_id=row.repository_id)
    write_target = _write_target(repo, installation)
    if write_target is None or target is None:
        raise LookupError("the repository or its deploy target is gone")
    return write_target, target.config.workflow_file


async def _fail(ctx: RemediationContext, state: RemediationState, dep: deployments.Deployment, reason: str, *, status: str = "failed") -> dict[str, Any]:
    remediation_id, _ = _ids(state)
    async with ctx.sessions() as session:
        await deployments.update_run(session, tenant_id=ctx.tenant_id, deployment_id=dep.id, status=status)  # type: ignore[arg-type]
        await remediation_store.transition(
            session, tenant_id=ctx.tenant_id, remediation_id=remediation_id, from_status="DEPLOYING", to="FAILED", outcome={"failed": reason}
        )
    await _audit(ctx, state, "deploy.failed", "failed", detail=reason)
    await _emit(ctx, state, "deploy.failed", reason=reason, runUrl=dep.run_url)
    return {"outcome": "failed", "wake_at": None}


async def trigger_deploy(state: RemediationState, runtime: Ctx) -> dict[str, Any]:
    """Ask the pipeline to deploy the merge commit, once. The deployment row carries the correlation id: a second attempt
    looks for the run with that id before it would dispatch, and a row that exists means a dispatch may already have happened."""
    ctx = runtime.context
    remediation_id, _ = _ids(state)
    target, workflow = await _target_and_config(ctx, state)
    async with ctx.sessions() as session:
        dep, created = await deployments.ensure(
            session, tenant_id=ctx.tenant_id, remediation_id=remediation_id, kind="deploy", environment=state["environment"], sha=state["merge_sha"], now=ctx.now()
        )
    if dep.run_id is not None or dep.finished:
        return {"outcome": "failed" if dep.status in ("failed", "cancelled", "timeout") else None}
    request = DispatchRequest(workflow, target.default_branch, state["merge_sha"], state["environment"], dep.correlation_id)
    try:
        run = await ctx.provider.find_run(target, request)
    except GitHubUnavailableError:
        run = None
    if run is None and created:
        await _audit(ctx, state, "deploy.dispatching", "ok", detail=f"workflow {workflow}", actor=_decider(state))
        try:
            await ctx.provider.dispatch(target, request)
        except (WriteRejectedError, GitHubAccessError):
            return await _fail(ctx, state, dep, "GitHub refused to start the workflow (does it accept workflow_dispatch, and has the App been granted actions: write?)")
        except GitHubUnavailableError:
            logger.warning("dispatch of deployment %s could not be confirmed; the run is looked for", dep.id)
        await _audit(ctx, state, "deploy.dispatched", "ok", detail=f"workflow {workflow}", actor=_decider(state))
        await _emit(ctx, state, "deploy.dispatched", sha=state["merge_sha"])
    if run is not None:
        async with ctx.sessions() as session:
            await deployments.update_run(session, tenant_id=ctx.tenant_id, deployment_id=dep.id, status=run.status, run_id=run.run_id, run_url=run.url)
    return {"outcome": None}


def _decider(state: RemediationState) -> UUID | None:
    raw = (state.get("approval") or {}).get("decided_by")
    try:
        return UUID(str(raw)) if raw else None
    except ValueError:
        return None


async def wait_deploy(state: RemediationState, runtime: Ctx) -> dict[str, Any]:
    """Look at the pipeline run once. Finished: record it (a success also enters the release ledger). Not finished: say when to
    look again (``wake_at``), or fail when the deadline passed or the run never appeared."""
    ctx = runtime.context
    remediation_id, _ = _ids(state)
    rule = _rule(state)
    target, workflow = await _target_and_config(ctx, state)
    async with ctx.sessions() as session:
        dep = await deployments.get(session, tenant_id=ctx.tenant_id, remediation_id=remediation_id, kind="deploy")
    assert dep is not None
    now = ctx.now()

    if dep.status == "succeeded":
        return await _succeeded(ctx, state, dep)
    if dep.finished:
        return await _fail(ctx, state, dep, f"the pipeline run ended as {dep.status}", status=dep.status)

    request = DispatchRequest(workflow, target.default_branch, state["merge_sha"], state["environment"], dep.correlation_id)
    try:
        run = await (ctx.provider.run_status(target, dep.run_id) if dep.run_id else ctx.provider.find_run(target, request))
    except GitHubUnavailableError:
        run = None  # a bad moment at GitHub: look again later, the deadline still applies
    except (GitHubAccessError, WriteRejectedError):
        return await _fail(ctx, state, dep, "GitHub no longer lets log0 read the pipeline run")
    if run is not None:
        async with ctx.sessions() as session:
            await deployments.update_run(session, tenant_id=ctx.tenant_id, deployment_id=dep.id, status=run.status, run_id=run.run_id, run_url=run.url)
        dep = replace(dep, run_id=run.run_id, run_url=run.url, status=run.status)
        if run.status == "succeeded":
            return await _succeeded(ctx, state, dep)
        if run.status in ("failed", "cancelled"):
            return await _fail(ctx, state, dep, f"the pipeline run {run.status}", status=run.status)
    elif now - dep.dispatched_at > DISPATCH_GRACE:
        return await _fail(ctx, state, dep, "the pipeline run never appeared after the dispatch")
    if now - dep.dispatched_at > timedelta(seconds=rule.deploy_timeout_seconds):
        if dep.run_id is not None:
            try:
                await ctx.provider.cancel(target, dep.run_id)
            except (GitHubAccessError, GitHubUnavailableError):
                logger.warning("could not cancel run %s of deployment %s", dep.run_id, dep.id)
        return await _fail(ctx, state, dep, "the deploy did not finish in time", status="timeout")
    return {"wake_at": _later(now, ctx.settings.deploy_poll_seconds)}


def _later(now: datetime, seconds: float) -> str:
    return (now + timedelta(seconds=seconds)).isoformat()


async def _succeeded(ctx: RemediationContext, state: RemediationState, dep: deployments.Deployment) -> dict[str, Any]:
    """The pipeline finished: the environment runs the commit. Recorded in the ledger, and the run moves on to be verified."""
    remediation_id, _ = _ids(state)
    async with ctx.sessions() as session:
        row = await remediation_store.get(session, tenant_id=ctx.tenant_id, remediation_id=remediation_id)
        assert row is not None
        await releases.record(
            session, tenant_id=ctx.tenant_id, repository_id=row.repository_id, environment=state["environment"], sha=state["merge_sha"], source="log0", remediation_id=remediation_id
        )
        await remediation_store.transition(session, tenant_id=ctx.tenant_id, remediation_id=remediation_id, from_status="DEPLOYING", to="VERIFYING", outcome={"deployed": True})
    await _audit(ctx, state, "deploy.succeeded", "ok", detail=dep.run_url or "")
    await _emit(ctx, state, "deploy.succeeded", runUrl=dep.run_url)
    return {"outcome": "deployed", "wake_at": None}


async def park(state: RemediationState, runtime: Ctx) -> dict[str, Any]:
    """Wait for a timer. The run stops here; the executor stores ``wake_at`` and the sweeper resumes it when it is due."""
    interrupt({"gate": WAIT_GATE, "wakeAt": state.get("wake_at")})
    return {"wake_at": None}
