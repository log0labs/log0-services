"""The golden dataset: what each evaluation case is, and what a correct investigation of it looks like.

A case is an incident fixture (recorded from a real run, see ``capture.py``), the repository ref to read, and
an ``Expected`` block, the answer key, written from the demo shop's ``bugs.yml``. The dataset is
``evals/golden/golden.yaml``; ``load_cases`` reads and validates it, so a typo in the key fails loudly
rather than quietly scoring everything zero.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

EVALS = Path(__file__).resolve().parent
GOLDEN = EVALS / "golden" / "golden.yaml"
FIXTURES = EVALS / "fixtures"

Category = Literal["dependency", "deployment", "resource", "code_bug", "config", "unknown"]


class Edit(BaseModel):
    """One exact replacement in one file: the shape of the reference fix."""

    file: str
    old: str
    new: str


class FixCheck(BaseModel):
    """A pattern a good fix diff shows: a regular expression that must match an added line and/or a removed line."""

    added: str | None = None
    removed: str | None = None

    @model_validator(mode="after")
    def _something(self) -> FixCheck:
        if self.added is None and self.removed is None:
            raise ValueError("a fix check needs 'added' or 'removed'")
        return self


class Expected(BaseModel):
    categories: list[Category] = Field(description="Triage labels that count as right (any of them)")
    cause_files: list[str] = Field(default_factory=list, description="Repository paths of the root cause; empty when it is not in the code")
    secondary_files: list[str] = Field(default_factory=list, description="Files a good answer may also touch or mention")
    cause_symbol: str | None = None
    fixable_by_code: bool = True
    reference: str = Field(description="The root cause, in words: what the judge compares against")
    keywords: list[list[str]] = Field(
        default_factory=list,
        description="Every group must be matched by at least one of its words in the answer (case-insensitive)",
    )
    trace_files: list[str] = Field(default_factory=list, description="Repository files the stack trace should lead to (any of)")
    fix_checks: list[FixCheck] = Field(default_factory=list)
    reference_fix: list[Edit] = Field(default_factory=list, description="A known-good fix: applied, it makes the case pass")
    must_not_contain: list[str] = Field(default_factory=list, description="Strings that must never appear in the output (canaries, secrets)")
    must_not_read: list[str] = Field(default_factory=list, description="File names the code tools must not be asked to read")


class Case(BaseModel):
    id: str
    title: str
    kind: Literal["bug", "canary"] = "bug"
    bug_id: int | None = None
    fixture: str
    service: str
    repo_ref: str
    question: str | None = None
    inject: str | None = Field(default=None, description="A log line planted in the incident's logs (the prompt-injection canary)")
    expected: Expected

    @property
    def fixture_dir(self) -> Path:
        return FIXTURES / self.fixture


class RetrievalCase(BaseModel):
    id: str
    query: str
    relevant: list[str] = Field(description="'<runbook file stem> > <heading path>' of the chunks that answer the query")


def load_cases(path: Path = GOLDEN) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [Case.model_validate(c) for c in data["cases"]]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in the golden dataset")
    return cases


def load_retrieval_cases(path: Path = EVALS / "golden" / "retrieval.yaml") -> list[RetrievalCase]:
    return [RetrievalCase.model_validate(c) for c in yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]]
