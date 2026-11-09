"""What the code investigator reports back to the investigation.

Two layers, as everywhere in this service. ``CodeFindingsAnswer`` is what the *model* is asked to
write (it carries the descriptions the model reads). ``CodeFindings`` is what the *code* hands on
after checking it: every location and commit in it was really seen by a tool, so a later step can
cite it without re-checking, and nothing the model invented survives.
"""

from typing import Literal

from pydantic import BaseModel, Field

FindingKind = Literal["suspect_code", "recent_change", "context"]


class CodeRef(BaseModel):
    path: str
    start_line: int
    end_line: int


class CodeFindingDraft(BaseModel):
    summary: str = Field(
        description="One or two plain sentences: what you found and why it matters to this incident. "
        "Only what the tool results showed."
    )
    kind: FindingKind = Field(
        description=(
            "suspect_code: code that plausibly causes the failure (for example the line that raises, or "
            "a missing timeout or null check). recent_change: a commit that touched the suspect code shortly "
            "before the incident. context: supporting background (a caller, a configuration constant)."
        )
    )
    locations: list[CodeRef] = Field(
        default_factory=list,
        description="Files and line ranges the finding is about, copied from tool results you actually saw.",
    )
    commits: list[str] = Field(
        default_factory=list,
        description="Commit ids (7 to 40 hex characters) the finding is about, copied from tool results you actually saw.",
    )


class CodeFindingsAnswer(BaseModel):
    """What the model writes when it has finished looking at the code."""

    findings: list[CodeFindingDraft] = Field(
        description="Up to five findings, most important first. Empty when the code showed nothing relevant: "
        "never invent one to fill the list. Every finding must cite at least one location or commit."
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="What you could not determine from the code and what would settle it. Empty if nothing.",
    )


class CodeFinding(BaseModel):
    summary: str
    kind: FindingKind
    locations: list[CodeRef] = Field(default_factory=list)
    commits: list[str] = Field(default_factory=list)  # full commit ids, each one seen in a tool result


class CodeFindings(BaseModel):
    findings: list[CodeFinding] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    tool_calls: int = 0  # every tool call made, seeding included: the parent adds it to its own budget
    steps: int = 0  # model turns
