"""The words log0 puts on GitHub: branch name, commit message, pull request title and body.

Pure functions. Everything that came from a model or a log passes through ``redact_text`` and is
length-capped, so a secret that slipped into an explanation cannot be published.
"""

from uuid import UUID

from app.diff.builder import FileDiff
from app.integrations.github_writer import BRANCH_PREFIX, commit_marker
from app.schemas.domain import CiRun, CodeChange, RootCause
from app.security.redaction import redact_text

MAX_TITLE = 100
MAX_SECTION = 1500
MAX_FILES_LISTED = 15
FOOTER = "Opened by log0 from an AI investigation. Review the change and the tests before merging; log0 never merges."


def branch_name(incident_id: UUID, investigation_id: UUID) -> str:
    """``log0/fix-<incident>-<investigation>`` with the first 8 characters of each id."""
    return f"{BRANCH_PREFIX}fix-{str(incident_id)[:8]}-{str(investigation_id)[:8]}"


def _clean(text: str, limit: int) -> str:
    text = " ".join(redact_text(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def pull_request_title(root_cause: RootCause) -> str:
    return "Fix: " + _clean(root_cause.statement, MAX_TITLE - len("Fix: "))


def commit_message(root_cause: RootCause, investigation_id: UUID) -> str:
    return f"{pull_request_title(root_cause)}\n\n{commit_marker(str(investigation_id))}\n"


def pull_request_body(
    *,
    root_cause: RootCause,
    confidence_band: str | None,
    changes: list[CodeChange],
    files: list[FileDiff],
    investigation_url: str | None = None,
    ci_run: CiRun | None = None,
) -> str:
    lines = ["## Root cause", "", _clean(root_cause.statement, MAX_SECTION), ""]
    if confidence_band:
        lines += [f"Confidence: **{confidence_band}**", ""]
    lines += ["## Changes", ""]
    explained = {c.file_path: c.explanation for c in changes}
    for file in files[:MAX_FILES_LISTED]:
        note = explained.get(file.file_path)
        suffix = f": {_clean(note, 200)}" if note else ""
        lines.append(f"- `{file.file_path}` ({file.operation}, +{file.additions} -{file.deletions}){suffix}")
    if len(files) > MAX_FILES_LISTED:
        lines.append(f"- and {len(files) - MAX_FILES_LISTED} more")
    lines += _tests_section(ci_run)
    if root_cause.evidence_ids:
        lines += ["", f"Evidence: {', '.join(f'`{e}`' for e in root_cause.evidence_ids[:10])}"]
    if investigation_url:
        lines += ["", f"Investigation: {investigation_url}"]
    lines += ["", "---", FOOTER]
    return "\n".join(lines)


_OUTCOME_TEXT = {
    "passed": "The checks of the repository **passed** on this commit.",
    "failed": "The checks of the repository **failed** on this commit. Do not merge without looking at them.",
    "timeout": "The checks were still running when log0 stopped waiting; look at their status before merging.",
    "none": "The repository has no checks for this commit, so **nothing verified this change**. Run the tests yourself.",
}


def _tests_section(ci_run: CiRun | None) -> list[str]:
    if ci_run is None:
        return ["", "## Tests", "", "log0 could not read the checks of the repository. Run the tests yourself."]
    lines = ["", "## Tests", "", _OUTCOME_TEXT[ci_run.outcome]]
    for check in ci_run.checks[:MAX_FILES_LISTED]:
        lines.append(f"- {_clean(check.name, 80)}: {check.conclusion or check.status}")
    return lines
