"""What log0 refuses to publish, however good the diff looks. Pure functions, no I/O.

A pull request is a write to somebody else's repository, proposed from text a model produced out of logs
that anyone may have written. These rules do not depend on the model behaving: files that change how
code is built, released or authorised are never touched, a change must stay small enough to be reviewed,
and a repository (or a workspace) cannot be flooded with pull requests.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.diff.builder import FileDiff

if TYPE_CHECKING:
    from app.config import Settings

_PROTECTED_DIRECTORIES = (".github", ".git", ".circleci", ".gitlab")
_PROTECTED_NAMES = frozenset(
    {
        "codeowners",
        "jenkinsfile",
        ".gitmodules",
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials.json",
        "service-account.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "cargo.lock",
        "go.sum",
        "poetry.lock",
        "uv.lock",
        "gemfile.lock",
        "composer.lock",
    }
)
_PROTECTED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
_MAX_REASONS_SHOWN = 3


@dataclass(frozen=True)
class PrLimits:
    max_files: int = 10
    max_changed_lines: int = 400  # additions plus deletions over the whole change
    max_per_repository_per_hour: int = 3
    max_per_tenant_per_hour: int = 10


@dataclass(frozen=True)
class PrActivity:
    """How many pull requests log0 opened lately, for the rate limits."""

    repository_last_hour: int = 0
    tenant_last_hour: int = 0


def protected_reason(path: str) -> str | None:
    """Why ``path`` may never be part of a log0 pull request, or ``None``."""
    normal = path.replace("\\", "/").strip("/").lower()
    parts = normal.split("/")
    name = parts[-1]
    if any(part in _PROTECTED_DIRECTORIES for part in parts[:-1]):
        return f"{path} is part of the build, release or repository configuration"
    if name in _PROTECTED_NAMES:
        return f"{path} is a protected file (pipeline, ownership, lock or credential configuration)"
    if name.startswith((".env", "id_rsa")) or name.endswith(_PROTECTED_SUFFIXES):
        return f"{path} may hold secrets"
    return None


def check_diff(files: list[FileDiff], limits: PrLimits) -> list[str]:
    """Reasons the change cannot be published as a pull request (empty: it can)."""
    reasons = [reason for file in files if (reason := protected_reason(file.file_path))]
    if len(files) > limits.max_files:
        reasons.append(f"it touches {len(files)} files and the limit is {limits.max_files}")
    changed = sum(file.additions + file.deletions for file in files)
    if changed > limits.max_changed_lines:
        reasons.append(f"it changes {changed} lines and the limit is {limits.max_changed_lines}")
    return reasons[:_MAX_REASONS_SHOWN]


def check_rate(activity: PrActivity, limits: PrLimits) -> list[str]:
    reasons = []
    if activity.repository_last_hour >= limits.max_per_repository_per_hour:
        reasons.append(f"log0 already opened {activity.repository_last_hour} pull requests on this repository in the last hour")
    if activity.tenant_last_hour >= limits.max_per_tenant_per_hour:
        reasons.append(f"this workspace already has {activity.tenant_last_hour} log0 pull requests in the last hour")
    return reasons


@dataclass(frozen=True)
class TenantRules:
    """What a workspace chose. ``None`` means "use the server's value"."""

    only_on_green: bool = False
    max_files: int | None = None
    max_changed_lines: int | None = None
    max_per_repository_per_hour: int | None = None
    max_per_tenant_per_hour: int | None = None


def _tighter(server: int, chosen: int | None) -> int:
    return server if chosen is None else min(server, chosen)


def effective_limits(server: PrLimits, rules: TenantRules | None) -> PrLimits:
    """The server's limits are ceilings: a workspace can make them stricter, never looser."""
    if rules is None:
        return server
    return PrLimits(
        max_files=_tighter(server.max_files, rules.max_files),
        max_changed_lines=_tighter(server.max_changed_lines, rules.max_changed_lines),
        max_per_repository_per_hour=_tighter(server.max_per_repository_per_hour, rules.max_per_repository_per_hour),
        max_per_tenant_per_hour=_tighter(server.max_per_tenant_per_hour, rules.max_per_tenant_per_hour),
    )


def effective_only_on_green(server: bool, rules: TenantRules | None) -> bool:
    """Either the server or the workspace can require passing checks; neither can switch the other off."""
    return server or bool(rules and rules.only_on_green)


def server_limits(settings: "Settings") -> PrLimits:
    """The limits the server's settings allow: the ceilings for every workspace."""
    return PrLimits(
        settings.pr_max_files,
        settings.pr_max_changed_lines,
        settings.pr_max_per_repository_per_hour,
        settings.pr_max_per_tenant_per_hour,
    )
