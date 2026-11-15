"""Persistence for ai.remediation: the offers a merge creates, and (from step 13) the runs a human starts from them.

Every function filters by ``tenant_id``. An offer is one row per pull request and environment, so a webhook that GitHub
delivers twice, or a pull request that is merged and the event replayed, creates each offer exactly once.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import remediation


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
