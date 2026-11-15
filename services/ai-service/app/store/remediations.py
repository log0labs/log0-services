"""Persistence for ai.remediation: the offers a merge creates, and (from step 13) the runs a human starts from them.

Every function filters by ``tenant_id``. An offer is one row per pull request and environment, so a webhook that GitHub
delivers twice, or a pull request that is merged and the event replayed, creates each offer exactly once.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import deployment, remediation
from app.schemas.remediation import TERMINAL_STATUSES, TRANSITIONS, can_transition


class EnvironmentBusyError(Exception):
    """Another remediation of this repository is already running in this environment."""


@dataclass(frozen=True)
class Remediation:
    id: UUID
    tenant_id: UUID
    pull_request_id: UUID
    investigation_id: UUID
    repository_id: UUID
    environment: str
    merge_sha: str
    rollback_target: str | None
    status: str
    baseline: dict[str, Any] | None
    policy: dict[str, Any] | None
    outcome: dict[str, Any] | None
    started_by: UUID | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    wake_at: datetime | None


def _to_remediation(row) -> Remediation:
    m = row._mapping
    return Remediation(**{name: m[name] for name in Remediation.__dataclass_fields__})


async def offer_for_merge(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    pull_request_id: UUID,
    investigation_id: UUID,
    repository_id: UUID,
    merge_sha: str,
    environments: list[str],
) -> list[Remediation]:
    """Offer a deploy of the merge commit to each of ``environments``; returns only the offers this call created.

    Idempotent: an offer that already exists (the webhook came twice) is left as it is and not returned.
    """
    if not environments:
        return []
    statement = (
        insert(remediation)
        .values(
            [
                {
                    "tenant_id": tenant_id,
                    "pull_request_id": pull_request_id,
                    "investigation_id": investigation_id,
                    "repository_id": repository_id,
                    "environment": environment,
                    "merge_sha": merge_sha,
                }
                for environment in environments
            ]
        )
        .on_conflict_do_nothing(index_elements=[remediation.c.pull_request_id, remediation.c.environment])
        .returning(remediation)
    )
    created = (await session.execute(statement)).all()
    await session.commit()
    return [_to_remediation(row) for row in created]


async def list_for_pull_request(session: AsyncSession, *, tenant_id: UUID, pull_request_id: UUID) -> list[Remediation]:
    result = await session.execute(
        select(remediation)
        .where(remediation.c.tenant_id == tenant_id, remediation.c.pull_request_id == pull_request_id)
        .order_by(remediation.c.created_at, remediation.c.environment)
    )
    return [_to_remediation(row) for row in result]


async def get(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID) -> Remediation | None:
    row = (await session.execute(select(remediation).where(remediation.c.tenant_id == tenant_id, remediation.c.id == remediation_id))).first()
    return _to_remediation(row) if row else None


async def list_for_investigation(session: AsyncSession, *, tenant_id: UUID, investigation_id: UUID) -> list[Remediation]:
    result = await session.execute(
        select(remediation).where(remediation.c.tenant_id == tenant_id, remediation.c.investigation_id == investigation_id).order_by(remediation.c.created_at, remediation.c.environment)
    )
    return [_to_remediation(row) for row in result]


async def start(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID, started_by: UUID) -> Remediation | None:
    """OFFERED to PREFLIGHT, which takes the environment. ``None`` if it is not an offer (a second click); ``EnvironmentBusyError`` if
    another remediation of the repository holds the environment (the partial unique index decides, not a check)."""
    try:
        row = (
            await session.execute(
                update(remediation)
                .where(remediation.c.tenant_id == tenant_id, remediation.c.id == remediation_id, remediation.c.status == "OFFERED")
                .values(status="PREFLIGHT", started_by=started_by, updated_at=func.now())
                .returning(remediation)
            )
        ).first()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise EnvironmentBusyError from None
    return _to_remediation(row) if row else None


async def transition(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    remediation_id: UUID,
    to: str,
    from_status: str | None = None,
    **fields: object,
) -> bool:
    """Move to ``to`` if the status diagram allows it; ``True`` only if this call made the move.

    With ``from_status`` the move happens only from that status (a conditional UPDATE: of two racing answers one wins). Without it,
    from any status that may move to ``to``. ``fields`` are written in the same statement (``outcome``, ``wake_at``, ...).
    """
    if from_status is not None:
        if not can_transition(from_status, to):
            raise ValueError(f"{from_status} cannot move to {to}")
        allowed = [from_status]
    else:
        allowed = [status for status, targets in TRANSITIONS.items() if to in targets]
    values = dict(fields)
    if to in TERMINAL_STATUSES:
        values["finished_at"] = func.now()
        values.setdefault("wake_at", None)
    result = await session.execute(
        update(remediation)
        .where(remediation.c.tenant_id == tenant_id, remediation.c.id == remediation_id, remediation.c.status.in_(allowed))
        .values(status=to, updated_at=func.now(), **values)
    )
    await session.commit()
    return result.rowcount > 0


async def revert(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID, current: str, back: str) -> bool:
    """Undo a move that could not be completed (an answer that could not be written): ``current`` goes back to ``back``.

    Not a transition of the status diagram on purpose: it is the compensation for a half-finished one, and it also clears
    the finish time a terminal move set.
    """
    result = await session.execute(
        update(remediation)
        .where(remediation.c.tenant_id == tenant_id, remediation.c.id == remediation_id, remediation.c.status == current)
        .values(status=back, finished_at=None, updated_at=func.now())
    )
    await session.commit()
    return result.rowcount > 0


async def set_fields(session: AsyncSession, *, tenant_id: UUID, remediation_id: UUID, **fields: object) -> None:
    """Write ``baseline``, ``policy``, ``rollback_target``, ``outcome`` or ``wake_at`` without changing the status."""
    await session.execute(
        update(remediation).where(remediation.c.tenant_id == tenant_id, remediation.c.id == remediation_id).values(updated_at=func.now(), **fields)
    )
    await session.commit()


async def due(session: AsyncSession, *, now: datetime, limit: int = 50) -> list[tuple[UUID, UUID]]:
    """``(remediation id, tenant id)`` of runs whose wake-up time has come. The sweeper is a platform process, so no tenant filter."""
    result = await session.execute(
        select(remediation.c.id, remediation.c.tenant_id).where(remediation.c.wake_at.is_not(None), remediation.c.wake_at <= now).order_by(remediation.c.wake_at).limit(limit)
    )
    return [(row[0], row[1]) for row in result]


async def interrupted(session: AsyncSession) -> list[tuple[UUID, UUID]]:
    """Runs that were mid-flight when the service stopped (not waiting for a human, not waiting for a timer)."""
    result = await session.execute(
        select(remediation.c.id, remediation.c.tenant_id).where(remediation.c.status.in_(["PREFLIGHT", "DEPLOYING"]), remediation.c.wake_at.is_(None))
    )
    return [(row[0], row[1]) for row in result]


async def deploys_since(session: AsyncSession, *, tenant_id: UUID, repository_id: UUID, environment: str, since: datetime) -> int:
    """How many deploys were dispatched to this environment of this repository since ``since`` (any outcome)."""
    query = (
        select(func.count())
        .select_from(deployment.join(remediation, deployment.c.remediation_id == remediation.c.id))
        .where(
            deployment.c.tenant_id == tenant_id,
            remediation.c.repository_id == repository_id,
            deployment.c.environment == environment,
            deployment.c.kind == "deploy",
            deployment.c.dispatched_at >= since,
        )
    )
    return int((await session.execute(query)).scalar_one())


async def verified(session: AsyncSession, *, tenant_id: UUID, pull_request_id: UUID, environment: str) -> bool:
    """Did this pull request's change verify in ``environment``? (Only a VERIFIED remediation counts.)"""
    row = (
        await session.execute(
            select(remediation.c.id).where(
                remediation.c.tenant_id == tenant_id,
                remediation.c.pull_request_id == pull_request_id,
                remediation.c.environment == environment,
                remediation.c.status == "VERIFIED",
            )
        )
    ).first()
    return row is not None
