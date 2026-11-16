"""Persistence for ai.test_run: what the repository's CI said about a commit log0 made."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import test_run
from app.schemas.domain import CiRun


async def record(session: AsyncSession, *, tenant_id: UUID, investigation_id: UUID, commit_sha: str, run: CiRun) -> None:
    await session.execute(
        insert(test_run).values(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            commit_sha=commit_sha,
            outcome=run.outcome,
            checks=[check.model_dump() for check in run.checks],
            finished_at=datetime.now(UTC),
        )
    )
    await session.commit()

