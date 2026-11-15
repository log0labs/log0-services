"""The pull-request gate through the runner, the executor and a real Postgres: pause, approve, publish, resume.

The in-memory checkpointer stands in for the Postgres one; a fake publisher records what would be written to
GitHub. ``decide`` does exactly what ``POST /pr/approve`` and ``/pr/decline`` do (flip the status, write the
decision event, resume the runner).
"""

import os
import shutil
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.session import create_session_factory
from app.graph.builder import build_graph
from app.graph.executor import GraphExecutor
from app.graph.gates import PR_APPROVAL
from app.runner import InvestigationRunner, SessionFactory
from app.schemas.change_proposal import ChangeProposalAnswer, CodeChangeDraft
from app.schemas.code_findings import CodeFindingDraft, CodeFindingsAnswer, CodeRef
from app.schemas.investigation import InvestigationStatus
from app.store import events as event_store
from app.store import investigations as store
from tests.code_graph_support import (
    PAYMENT,
    FakeCode,
    Repo,
    ToolFakeModel,
    ai,
    call,
    checkout_for,
    context_with_code,
)
from tests.graph_fakes import (
    FakeApi,
    FakeHistory,
    FakeLogs,
    incident_payload,
    synthesis_answer,
)
from tests.pull_request_support import APPROVER, GOOD, FakePublisher
from tests.test_graph_executor import TENANT, _create, _events, _row, _wait_for_end

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
    pytest.mark.asyncio,
]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    return Repo(tmp_path_factory.mktemp("pr-flow") / "repo")


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[SessionFactory]:
    engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        async with factory() as session:
            await session.execute(text("DELETE FROM ai.investigation WHERE tenant_id = :t"), {"t": TENANT})
            await session.commit()
        await engine.dispose()


def make_executor(repo: Repo, publisher: FakePublisher, saver: InMemorySaver) -> GraphExecutor:
    model = ToolFakeModel(
        synthesis_answer(),
        script=[ai(call("list_files", "c1")), AIMessage(content="investigated")],
        answers={
            CodeFindingsAnswer: CodeFindingsAnswer(
                findings=[CodeFindingDraft(summary="The gateway call has no timeout.", kind="suspect_code", locations=[CodeRef(path=PAYMENT, start_line=118, end_line=122)])]
            ),
            ChangeProposalAnswer: ChangeProposalAnswer(
                changes=[CodeChangeDraft(file_path=GOOD.file_path, operation=GOOD.operation, explanation=GOOD.explanation, old_code=GOOD.old_code, new_code=GOOD.new_code)]
            ),
        },
    )
    _, models = context_with_code(FakeCode(checkout_for(repo)), investigation=model)
    return GraphExecutor(
        build_graph(saver),
        logs=FakeLogs(),
        incidents=FakeApi(incident_payload(tenantId=str(TENANT))),
        history=FakeHistory(),
        models=models,
        code=FakeCode(checkout_for(repo)),
        publisher=publisher,
    )


async def decide(factory: SessionFactory, runner: InvestigationRunner, investigation_id: UUID, action: str, *, schedule: bool = True) -> bool:
    """What the pull-request endpoints do. Returns whether this call won the AWAITING_PR_APPROVAL change."""
    async with factory() as session:
        won = await store.transition_status_if(
            session,
            investigation_id=investigation_id,
            tenant_id=TENANT,
            from_status=InvestigationStatus.AWAITING_PR_APPROVAL,
            to_status=PR_APPROVAL.resumed(action),
        )
        if not won:
            return False
        await event_store.append_event(session, investigation_id=investigation_id, type="pr.decided", payload={"action": action, "decidedBy": str(APPROVER)})
    if schedule:
        await runner.wait_idle(investigation_id)
        runner.schedule(investigation_id, tenant_id=TENANT)
    return True


async def run_to_pause(factory: SessionFactory, repo: Repo, publisher: FakePublisher, saver: InMemorySaver | None = None) -> tuple[InvestigationRunner, Any, InMemorySaver]:
    saver = saver or InMemorySaver()
    row = await _create(factory)
    runner = InvestigationRunner(factory, execute=make_executor(repo, publisher, saver).run)
    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)
    return runner, row, saver


def types(events: list[Any]) -> list[str]:
    return [e.type for e in events]


async def test_a_validated_diff_parks_the_run_at_the_pull_request_gate(session_factory: SessionFactory, repo: Repo) -> None:
    publisher = FakePublisher()
    runner, row, _ = await run_to_pause(session_factory, repo, publisher)

    waiting = await _row(session_factory, row.id)
    assert waiting.status == InvestigationStatus.AWAITING_PR_APPROVAL and waiting.completed_at is None
    assert waiting.diff is not None  # the reviewed diff is already on the row
    assert not runner.is_running(row.id)
    events = await _events(session_factory, row.id)
    request = next(e for e in events if e.type == "pr.approval_requested").payload
    assert request["repository"] == "acme/shop" and request["branch"].startswith("log0/fix-") and request["files"][0]["path"] == PAYMENT
    assert "investigation.completed" not in types(events) and types(events).count("pr.approval_requested") == 1
    assert publisher.names() == ["target_for", "activity"]
    await runner.shutdown()


async def test_a_parked_run_blocks_a_second_investigation_of_the_incident(session_factory: SessionFactory, repo: Repo) -> None:
    from app.store.exceptions import ActiveInvestigationError

    runner, row, _ = await run_to_pause(session_factory, repo, FakePublisher())

    with pytest.raises(ActiveInvestigationError):
        await _create(session_factory, incident_id=row.incident_id)
    await runner.shutdown()


async def test_approval_publishes_and_completes(session_factory: SessionFactory, repo: Repo) -> None:
    publisher = FakePublisher()
    runner, row, _ = await run_to_pause(session_factory, repo, publisher)

    assert await decide(session_factory, runner, row.id, "approve")
    await _wait_for_end(runner, row.id)

    done = await _row(session_factory, row.id)
    assert done.status == InvestigationStatus.COMPLETED and done.completed_at is not None
    events = await _events(session_factory, row.id)
    assert [t for t in types(events) if t in ("pr.approval_requested", "pr.decided", "branch.created", "commit.created", "pr.opened", "investigation.completed")] == [
        "pr.approval_requested",
        "pr.decided",
        "branch.created",
        "commit.created",
        "pr.opened",
        "investigation.completed",
    ]
    opened = next(e for e in events if e.type == "pr.opened").payload
    assert opened == {"number": 12, "url": "https://github.com/acme/shop/pull/12", "branch": opened["branch"], "draft": True}
    assert publisher.arguments("create_branch")["approved_by"] == APPROVER  # the approver's id reached the write
    assert [n for n in publisher.names() if n not in ("target_for", "activity", "check_results")] == ["create_branch", "commit", "open_pull_request"]
    await runner.shutdown()


async def test_decline_completes_without_writing(session_factory: SessionFactory, repo: Repo) -> None:
    publisher = FakePublisher()
    runner, row, _ = await run_to_pause(session_factory, repo, publisher)

    assert await decide(session_factory, runner, row.id, "decline")
    await _wait_for_end(runner, row.id)

    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    assert not {"create_branch", "commit", "open_pull_request"} & set(publisher.names())
    assert not {"branch.created", "pr.opened"} & set(types(await _events(session_factory, row.id)))
    await runner.shutdown()


async def test_only_one_of_two_answers_wins(session_factory: SessionFactory, repo: Repo) -> None:
    runner, row, _ = await run_to_pause(session_factory, repo, FakePublisher())

    assert await decide(session_factory, runner, row.id, "approve", schedule=False)
    assert not await decide(session_factory, runner, row.id, "approve", schedule=False)
    assert not await decide(session_factory, runner, row.id, "decline", schedule=False)
    await runner.shutdown()


async def test_a_restart_after_the_answer_resumes_with_that_answer_and_writes_once(session_factory: SessionFactory, repo: Repo) -> None:
    publisher = FakePublisher()
    runner, row, saver = await run_to_pause(session_factory, repo, publisher)
    assert await decide(session_factory, runner, row.id, "approve", schedule=False)  # answered, then the service stopped
    await runner.shutdown()
    assert (await _row(session_factory, row.id)).status == InvestigationStatus.PUBLISHING

    restarted = InvestigationRunner(session_factory, execute=make_executor(repo, publisher, saver).run)
    await restarted.recover_on_startup()
    await _wait_for_end(restarted, row.id)

    assert (await _row(session_factory, row.id)).status == InvestigationStatus.COMPLETED
    assert [n for n in publisher.names() if n not in ("target_for", "activity", "check_results")] == ["create_branch", "commit", "open_pull_request"]
    await restarted.shutdown()


async def test_a_restart_while_parked_keeps_waiting_for_the_engineer(session_factory: SessionFactory, repo: Repo) -> None:
    runner, row, saver = await run_to_pause(session_factory, repo, FakePublisher())
    await runner.shutdown()

    restarted = InvestigationRunner(session_factory, execute=make_executor(repo, FakePublisher(), saver).run)
    await restarted.recover_on_startup()
    await _wait_for_end(restarted, row.id)

    assert (await _row(session_factory, row.id)).status == InvestigationStatus.AWAITING_PR_APPROVAL
    await restarted.shutdown()


async def test_without_a_publisher_the_run_ends_with_its_diff_as_before(session_factory: SessionFactory, repo: Repo) -> None:
    saver = InMemorySaver()
    row = await _create(session_factory)
    executor = make_executor(repo, FakePublisher(allowed=False), saver)
    runner = InvestigationRunner(session_factory, execute=executor.run)

    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_for_end(runner, row.id)

    done = await _row(session_factory, row.id)
    assert done.status == InvestigationStatus.COMPLETED and done.diff is not None
    assert "pr.approval_requested" not in types(await _events(session_factory, row.id))
    await runner.shutdown()
