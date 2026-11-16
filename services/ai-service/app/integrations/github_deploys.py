"""Reads about deployments through the deploy-scoped installation token (``DEPLOY_PERMISSIONS``).

Deploying is a different capability from writing a commit, so it has its own token: these calls are made with
``actions`` and ``deployments`` and can neither write a commit nor open a pull request. Dispatching a workflow
joins this class in step 13.3.
"""

import re
from typing import Protocol

from app.integrations.github import GitHubUnavailableError
from app.integrations.github_app import DEPLOY_PERMISSIONS
from app.integrations.github_writer import GitHubWriter, WriteTarget

_SHA = re.compile(r"^[0-9a-f]{40}$")
DEPLOYMENTS_LOOKED_AT = 10  # newest first; a repository that deployed ten times without a success has no known release


class DeploysPort(Protocol):
    async def last_successful_deployment(self, target: WriteTarget, environment: str) -> str | None: ...


class GitHubDeploys:
    def __init__(self, writer: GitHubWriter) -> None:
        self._writer = writer

    async def last_successful_deployment(self, target: WriteTarget, environment: str) -> str | None:
        """The commit of the newest deployment to ``environment`` whose latest status is ``success``, as GitHub records it.

        ``None`` when GitHub has no such deployment: many pipelines never create one, so "unknown" is a normal answer.
        """
        repo = self._writer._repo(target)
        async with self._writer.session(target, DEPLOY_PERMISSIONS) as call:
            listing = await call("GET", f"{repo}/deployments", params={"environment": environment, "per_page": str(DEPLOYMENTS_LOOKED_AT)})
            try:
                deployments = [(int(row["id"]), str(row["sha"])) for row in listing.json()][:DEPLOYMENTS_LOOKED_AT]
            except (KeyError, TypeError, ValueError):
                raise GitHubUnavailableError from None
            for deployment_id, sha in deployments:
                statuses = await call("GET", f"{repo}/deployments/{deployment_id}/statuses", params={"per_page": "1"})
                try:
                    latest = statuses.json()
                    state = str(latest[0]["state"]) if latest else ""
                except (KeyError, TypeError, IndexError):
                    raise GitHubUnavailableError from None
                if state == "success" and _SHA.fullmatch(sha):
                    return sha
        return None
