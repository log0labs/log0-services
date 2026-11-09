"""Shared pieces for tests that run the code branch inside the investigation graph.

A real throwaway git repository (the one of the code investigator tests), a ``CodeAccess`` fake that
points at it, and a fake investigation model that can both answer structured requests (synthesize,
verify, root cause, code findings) and play a script of tool calls for the code investigator.
"""

import dataclasses
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage

from app.graph.code_access import CodeCheckout
from app.graph.state import InvestigationContext
from app.schemas.domain import RepoContext
from app.tools.code import CodeTools
from app.tools.git import GitTools
from tests.graph_fakes import FakeModel, FakeModels, make_context, triage_answer
from tests.test_code_investigator import (
    PAYMENT,
    TRACE,
    Repo,
    ai,
    call,
)

__all__ = ["PAYMENT", "TRACE", "FakeCode", "Repo", "ToolFakeModel", "ai", "call", "checkout_for", "context_with_code"]


class FakeCode:
    """A ``CodeAccess`` that returns a prepared checkout (or raises) and records how it was asked."""

    def __init__(self, result: CodeCheckout | Exception | None) -> None:
        self.result = result
        self.calls: list[tuple[UUID, str]] = []

    async def open(self, *, tenant_id: UUID, service_name: str) -> CodeCheckout | None:
        self.calls.append((tenant_id, service_name))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def checkout_for(repo: Repo, *, prefix: str = "services/payment", sha: str | None = None) -> CodeCheckout:
    return CodeCheckout(
        repo=RepoContext(repo="acme/shop", branch="main", sha=sha or repo.c2, path_prefix=prefix),
        code=CodeTools(repo.root, prefix=prefix),
        git=GitTools(repo.root, prefix=prefix),
    )


class _Bound:
    """What ``bind_tools`` returns: plays the model's script of tool-calling turns."""

    def __init__(self, owner: "ToolFakeModel") -> None:
        self.owner = owner

    async def ainvoke(self, messages: Any, *args: Any, **kwargs: Any) -> AIMessage:
        self.owner.tool_prompts.append(list(messages))
        step = self.owner.script.pop(0) if self.owner.script else AIMessage(content="done")
        if isinstance(step, Exception):
            raise step
        return step


class ToolFakeModel(FakeModel):
    """``FakeModel`` plus tool calling: ``script`` is a list of AI messages (or exceptions)."""

    def __init__(self, answer: Any = None, *, script: list[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(answer, **kwargs)
        self.script = script or []
        self.tool_prompts: list[list[Any]] = []
        self.bound: list[str] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> _Bound:
        self.bound = [t.name for t in tools]
        return _Bound(self)


def context_with_code(
    code: Any,
    *,
    investigation: ToolFakeModel | None = None,
    gather: list[str] | None = None,
    **kwargs: Any,
) -> tuple[InvestigationContext, FakeModels]:
    """A graph context whose triage asks for the code (unless ``gather`` says otherwise)."""
    models = FakeModels(FakeModel(triage_answer(gather=gather or ["logs", "history", "code"])), investigation=investigation)
    return dataclasses.replace(make_context(models=models, **kwargs), code=code), models
