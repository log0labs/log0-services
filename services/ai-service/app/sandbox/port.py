"""The graph's view of the test sandbox: one question, one answer, no Docker."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

SandboxStatus = Literal["passed", "failed", "timeout", "error"]


@dataclass(frozen=True)
class SandboxRun:
    """What happened when a repository's tests ran in the sandbox."""

    name: str  # for example "go test ./..."
    status: SandboxStatus  # error: the sandbox itself failed (not the tests)
    exit_code: int | None
    output_tail: str  # the last lines of output, redacted
    seconds: float


class Sandbox(Protocol):
    async def run(self, *, source: Path, subdir: str) -> SandboxRun | None:
        """Run the tests of the project in ``source/subdir`` (a disposable copy is made; ``source`` is never touched).

        ``None`` when the project's toolchain is not one the sandbox knows, so there is nothing to run.
        """
        ...
