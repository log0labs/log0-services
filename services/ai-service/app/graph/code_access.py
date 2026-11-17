"""How the graph gets at a tenant's code: a small port, so the graph never knows about databases,
encryption or git.

``CodeAccess.open`` answers one question for the ``gather_code`` node: "for this tenant's service,
what is the checked-out repository, which commit is it at, and what are the tools over it?".
Production wires ``DbCodeAccess`` (``integrations/code_access.py``: the 3.1 tables, the 3.2 secret
box, the 3.4 cache). Tests wire a fake that points at a temporary repository.
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, is_dataclass, replace
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID

from app.schemas.domain import RepoContext
from app.tools.code import CodeTools
from app.tools.git import GitTools


class CodeAccessError(Exception):
    """The repository exists but cannot be used right now (token unreadable, clone failed, ...).

    The message is written to be shown to the user inside a gap, so it never contains a secret.
    """


def shared_prefix(prefix: str, siblings: tuple[str, ...]) -> str:
    """The folder that holds ``prefix`` and every sibling folder: where a failure that crosses services can be searched.

    ``""`` (the whole repository) stays ``""``, and with no siblings nothing changes.
    """
    if not prefix or not siblings:
        return prefix
    parts = [PurePosixPath(p).parts for p in (prefix, *siblings) if p]
    common: list[str] = []
    for column in zip(*parts, strict=False):
        if len(set(column)) != 1:
            break
        common.append(column[0])
    return "/".join(common)


@dataclass(frozen=True)
class CodeCheckout:
    """A repository ready to be read: where and at which commit, and the tools over it."""

    repo: RepoContext  # owner/name, branch, the commit sha everything was read at, the service folder
    code: CodeTools
    git: GitTools
    siblings: tuple[str, ...] = ()  # the folders of the other services mapped to the same repository

    def widened(self) -> "CodeCheckout":
        """The same checkout whose default scope also covers the sibling services, because a failure can start in one
        service and show in another. A tool can still read any file by path; this widens what listing and searching see."""
        prefix = shared_prefix(self.repo.path_prefix, self.siblings)
        if prefix == self.repo.path_prefix or not (hasattr(self.code, "prefix") and hasattr(self.git, "prefix") and is_dataclass(self.code) and is_dataclass(self.git)):
            return self
        return replace(self, code=replace(self.code, prefix=prefix), git=replace(self.git, prefix=prefix))


class CodeAccess(Protocol):
    async def open(self, *, tenant_id: UUID, service_name: str) -> CodeCheckout | None:
        """The checkout for a service of this tenant, or ``None`` when no repository is mapped to it.

        Raises ``CodeAccessError`` when one is mapped but cannot be opened.
        """
        ...

    def snapshot(self, *, tenant_id: UUID, service_name: str, sha: str) -> AbstractAsyncContextManager[CodeCheckout | None]:
        """``async with`` a private copy of the repository at exactly ``sha`` (a full commit id).

        Unlike ``open`` it never fetches and never moves: it is what a later step uses to read, and
        patch, the very commit the investigation pinned, however far the shared checkout has moved on.
        The copy is removed when the block ends. Yields ``None`` when no repository is mapped; raises
        ``CodeAccessError`` when it cannot be provided (token unreadable, commit no longer available).
        """
        ...
