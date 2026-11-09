"""How the graph gets at a tenant's code: a small port, so the graph never knows about databases,
encryption or git.

``CodeAccess.open`` answers one question for the ``gather_code`` node: "for this tenant's service,
what is the checked-out repository, which commit is it at, and what are the tools over it?".
Production wires ``DbCodeAccess`` (``integrations/code_access.py``: the 3.1 tables, the 3.2 secret
box, the 3.4 cache). Tests wire a fake that points at a temporary repository.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas.domain import RepoContext
from app.tools.code import CodeTools
from app.tools.git import GitTools


class CodeAccessError(Exception):
    """The repository exists but cannot be used right now (token unreadable, clone failed, ...).

    The message is written to be shown to the user inside a gap, so it never contains a secret.
    """


@dataclass(frozen=True)
class CodeCheckout:
    """A repository ready to be read: where and at which commit, and the tools over it."""

    repo: RepoContext  # owner/name, branch, the commit sha everything was read at, the service folder
    code: CodeTools
    git: GitTools


class CodeAccess(Protocol):
    async def open(self, *, tenant_id: UUID, service_name: str) -> CodeCheckout | None:
        """The checkout for a service of this tenant, or ``None`` when no repository is mapped to it.

        Raises ``CodeAccessError`` when one is mapped but cannot be opened.
        """
        ...
