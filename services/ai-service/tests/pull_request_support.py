"""Shared pieces for the V2 tests: a recording publisher fake and a state that holds a reviewed, validated diff."""

import dataclasses
from typing import Any
from uuid import UUID, uuid4

from app.graph.nodes.validate_changes import validate_changes
from app.graph.state import InvestigationContext, InvestigationState
from app.publishing.naming import branch_name
from app.publishing.policy import PrActivity
from app.publishing.port import PublishError, PublishFile, PublishTarget
from app.schemas.domain import BranchRef, CheckResult, CiRun, CodeChange, PullRequestRef
from tests.code_graph_support import (
    PAYMENT,
    FakeCode,
    Repo,
    checkout_for,
    context_with_code,
)
from tests.test_change_proposer import NEW, OLD
from tests.test_propose_changes import approved_state
from tests.test_validate_changes import run_node

GOOD = CodeChange(file_path=PAYMENT, operation="modify", explanation="Pass the timeout again.", old_code=OLD, new_code=NEW)
APPROVER = UUID("44444444-4444-4444-8444-444444444444")
COMMIT_SHA = "c" * 40


class FakePublisher:
    """Allows writing (or not), returns canned results, records every call and its arguments."""

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, PublishError] = {}
        self.checks: list[list[CheckResult]] = [[CheckResult(name="ci / test", status="completed", conclusion="success")]]
        self.recorded: list[CiRun] = []
        self.recent = PrActivity()

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        if name in self.fail:
            raise self.fail[name]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def arguments(self, name: str) -> dict[str, Any]:
        return next(kwargs for call, kwargs in self.calls if call == name)

    async def target_for(self, *, tenant_id: UUID, repo: str) -> PublishTarget | None:
        self.calls.append(("target_for", {"repo": repo}))
        return PublishTarget(uuid4(), repo, "main") if self.allowed else None

    async def create_branch(self, *, tenant_id: UUID, target: PublishTarget, investigation_id: UUID, incident_id: UUID, base_sha: str, approved_by: UUID) -> BranchRef:
        self._record("create_branch", approved_by=approved_by, base_sha=base_sha)
        return BranchRef(name=branch_name(incident_id, investigation_id), base_sha=base_sha)

    async def commit(self, *, tenant_id: UUID, target: PublishTarget, investigation_id: UUID, branch: BranchRef, files: list[PublishFile], message: str) -> str:
        self._record("commit", files=files, message=message, branch=branch)
        return COMMIT_SHA

    async def open_pull_request(self, *, tenant_id: UUID, target: PublishTarget, investigation_id: UUID, branch: BranchRef, commit_sha: str, title: str, body: str, labels: list[str]) -> PullRequestRef:
        self._record("open_pull_request", title=title, body=body, commit_sha=commit_sha, labels=labels)
        return PullRequestRef(number=12, url="https://github.com/acme/shop/pull/12", branch=branch.name, commit_sha=commit_sha)


    async def activity(self, *, tenant_id: UUID, target: PublishTarget) -> PrActivity:
        self.calls.append(("activity", {}))
        return self.recent

    async def check_results(self, *, tenant_id: UUID, target: PublishTarget, commit_sha: str) -> list[CheckResult]:
        self._record("check_results", commit_sha=commit_sha)
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]

    async def record_ci_run(self, *, tenant_id: UUID, investigation_id: UUID, commit_sha: str, run: CiRun) -> None:
        self.recorded.append(run)


async def reviewed_state(repo: Repo, publisher: FakePublisher | None = None) -> tuple[InvestigationState, InvestigationContext, FakeCode]:
    """A state after V1 finished: a validated diff for ``GOOD`` at the pinned commit, with a publisher in the context."""
    code = FakeCode(checkout_for(repo))
    ctx, _ = context_with_code(code)
    ctx = dataclasses.replace(ctx, publisher=publisher or FakePublisher())
    base = approved_state(
        repo,
        proposed_changes=[GOOD],
        read_paths=[PAYMENT],
        change_attempts=1,
        investigation_id=str(uuid4()),
        incident_id=str(uuid4()),
        confidence_band="high",
    )
    state = await run_node(validate_changes, "validate_changes", base, ctx)
    assert state["diff"], "the fixture must hold a valid diff"
    return state, ctx, code
