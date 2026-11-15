"""``/api/v1/remediations``: the deploy offers a merge made, and the runs a human starts from them.

Anyone in the workspace may read. Starting an offer needs ADMIN or ENGINEER and only runs the preflight: nothing is
deployed until somebody with at least the environment's approver role answers the gate (ADMIN for production). Two
answers at once cannot both win: the move out of the waiting status is one conditional UPDATE. The answer is written to the
approval table before the run is resumed, so it survives a restart between the click and the resume.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deploy.nodes import MAX_EXTENSIONS
from app.deploy.policy import can_approve
from app.deploy.target import EnvironmentRule
from app.schemas.remediation_api import (
    DecisionIn,
    DeploymentOut,
    RemediationOut,
    VerificationOut,
)
from app.security.jwt import CurrentUser, get_current_user, require_investigation_writer
from app.store import (
    approvals,
    deploy_audit,
    deploy_targets,
    deployments,
    verifications,
)
from app.store import remediations as remediation_store

router = APIRouter(prefix="/api/v1/remediations", tags=["remediation"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _out(session: AsyncSession, row: remediation_store.Remediation) -> RemediationOut:
    dep = await deployments.get(session, tenant_id=row.tenant_id, remediation_id=row.id, kind="deploy")
    verdict = await verifications.latest(session, tenant_id=row.tenant_id, remediation_id=row.id)
    return RemediationOut(
        id=row.id,
        pull_request_id=row.pull_request_id,
        investigation_id=row.investigation_id,
        repository_id=row.repository_id,
        environment=row.environment,
        merge_sha=row.merge_sha,
        rollback_target=row.rollback_target,
        status=row.status,
        baseline=row.baseline,
        policy=row.policy,
        outcome=row.outcome,
        wake_at=row.wake_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
        verification=VerificationOut(verdict=verdict.verdict, numbers=verdict.numbers, window_end=verdict.window_end, created_at=verdict.created_at) if verdict else None,
        deployment=DeploymentOut(kind=dep.kind, status=dep.status, sha=dep.sha, run_url=dep.run_url, dispatched_at=dep.dispatched_at, finished_at=dep.finished_at) if dep else None,
    )


async def _owned(session: AsyncSession, user: CurrentUser, remediation_id: UUID) -> remediation_store.Remediation:
    row = await remediation_store.get(session, tenant_id=user.tenant_id, remediation_id=remediation_id)
    if row is None:
        raise _NOT_FOUND
    return row


@router.get("", response_model=list[RemediationOut])
async def list_remediations(
    investigation_id: Annotated[UUID, Query(alias="investigationId")],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[RemediationOut]:
    rows = await remediation_store.list_for_investigation(session, tenant_id=user.tenant_id, investigation_id=investigation_id)
    return [await _out(session, row) for row in rows]


@router.get("/{remediation_id}", response_model=RemediationOut)
async def get_remediation(
    remediation_id: UUID,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RemediationOut:
    return await _out(session, await _owned(session, user, remediation_id))


@router.post("/{remediation_id}/start", status_code=status.HTTP_202_ACCEPTED, response_model=RemediationOut)
async def start_remediation(
    remediation_id: UUID,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RemediationOut:
    """Run the preflight of an offer: the policy, the baseline, then the approval gate. Deploys nothing."""
    await _owned(session, user, remediation_id)
    try:
        started = await remediation_store.start(session, tenant_id=user.tenant_id, remediation_id=remediation_id, started_by=user.user_id)
    except remediation_store.EnvironmentBusyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Another deploy of this repository is already running in this environment") from None
    if started is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This deploy is not waiting to be started")
    await deploy_audit.append(session, tenant_id=user.tenant_id, remediation_id=remediation_id, action="remediation.started", result="ok", actor_id=user.user_id, environment=started.environment, sha=started.merge_sha)
    request.app.state.remediation_runner.schedule(remediation_id, user.tenant_id)
    return await _out(session, started)


async def _approver_rule(session: AsyncSession, user: CurrentUser, row: remediation_store.Remediation) -> EnvironmentRule:
    """The environment's rule, after checking the caller may act on it: 409 when the target is gone, 403 below the approver role."""
    target = None
    try:
        target = await deploy_targets.get(session, tenant_id=user.tenant_id, repository_id=row.repository_id)
    except ValidationError:
        pass
    rule = target.config.rule_for(row.environment) if target else None
    if rule is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This repository no longer has a deploy target for this environment")
    if not can_approve(user.role.value, rule):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Deploying to {row.environment} needs the {rule.approver} role")
    return rule


async def _answer(action: Literal["approve", "decline"], remediation_id: UUID, request: Request, user: CurrentUser, session: AsyncSession) -> RemediationOut:
    row = await _owned(session, user, remediation_id)
    await _approver_rule(session, user, row)
    moved_to = "DEPLOYING" if action == "approve" else "DECLINED"
    won = await remediation_store.transition(session, tenant_id=user.tenant_id, remediation_id=remediation_id, from_status="AWAITING_DEPLOY_APPROVAL", to=moved_to)
    if not won:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This deploy is not waiting for a decision")
    try:
        await approvals.record(session, tenant_id=user.tenant_id, remediation_id=remediation_id, gate="deploy", decision=action, user_id=user.user_id, role=user.role.value, environment=row.environment, sha=row.merge_sha)
        await deploy_audit.append(session, tenant_id=user.tenant_id, remediation_id=remediation_id, action=f"deploy.{action}d", result="ok", actor_id=user.user_id, environment=row.environment, sha=row.merge_sha)
    except Exception:  # noqa: BLE001 - whatever went wrong, the status must be put back
        await session.rollback()
        await remediation_store.revert(session, tenant_id=user.tenant_id, remediation_id=remediation_id, current=moved_to, back="AWAITING_DEPLOY_APPROVAL")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The decision could not be recorded; try again") from None
    runner = request.app.state.remediation_runner
    await runner.wait_idle(remediation_id)
    runner.schedule(remediation_id, user.tenant_id)
    return await _out(session, await _owned(session, user, remediation_id))


@router.post("/{remediation_id}/deploy/approve", status_code=status.HTTP_202_ACCEPTED, response_model=RemediationOut)
async def approve_deploy(
    remediation_id: UUID,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RemediationOut:
    """Let log0 ask the pipeline to deploy the merge commit to this environment."""
    return await _answer("approve", remediation_id, request, user, session)


@router.post("/{remediation_id}/deploy/decline", status_code=status.HTTP_202_ACCEPTED, response_model=RemediationOut)
async def decline_deploy(
    remediation_id: UUID,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RemediationOut:
    """Do not deploy. The merged pull request is untouched."""
    return await _answer("decline", remediation_id, request, user, session)


@router.post("/{remediation_id}/decision", status_code=status.HTTP_202_ACCEPTED, response_model=RemediationOut)
async def decide_verdict(
    remediation_id: UUID,
    body: DecisionIn,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_investigation_writer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> RemediationOut:
    """What to do about a verdict that is degraded or unclear: ``keep`` the deploy as it is, or ``extend`` the soak (twice at most)."""
    row = await _owned(session, user, remediation_id)
    await _approver_rule(session, user, row)
    if body.action == "extend" and await approvals.count(session, tenant_id=user.tenant_id, remediation_id=remediation_id, gate="decision", decision="extend") >= MAX_EXTENSIONS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The soak has already been extended as often as it may be")
    moved_to = "VERIFIED" if body.action == "keep" else "VERIFYING"
    won = await remediation_store.transition(session, tenant_id=user.tenant_id, remediation_id=remediation_id, from_status="AWAITING_DECISION", to=moved_to)
    if not won:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This deploy is not waiting for a decision")
    try:
        await approvals.record(session, tenant_id=user.tenant_id, remediation_id=remediation_id, gate="decision", decision=body.action, user_id=user.user_id, role=user.role.value, environment=row.environment, sha=row.merge_sha)
        await deploy_audit.append(session, tenant_id=user.tenant_id, remediation_id=remediation_id, action=f"deploy.verdict_{body.action}", result="ok", actor_id=user.user_id, environment=row.environment, sha=row.merge_sha)
    except Exception:  # noqa: BLE001 - whatever went wrong, the status must be put back
        await session.rollback()
        await remediation_store.revert(session, tenant_id=user.tenant_id, remediation_id=remediation_id, current=moved_to, back="AWAITING_DECISION")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The decision could not be recorded; try again") from None
    runner = request.app.state.remediation_runner
    await runner.wait_idle(remediation_id)
    runner.schedule(remediation_id, user.tenant_id)
    return await _out(session, await _owned(session, user, remediation_id))
