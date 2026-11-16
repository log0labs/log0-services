"""Check the proposed changes against the pinned commit, and build the diff git will accept.

``propose_changes`` (4.2) guarantees a change is well-formed and touches a file the model read.
This node (4.3 and 4.4 in the graph) checks it against **the files themselves**:

1. take a fresh snapshot of the repository at the pinned commit,
2. ``validate_changes`` (the validator): every file exists or not as the operation says, ``old_code``
   occurs exactly once, the files were read, edits do not overlap,
3. if all of that holds, build the unified diff and run ``git apply --check`` on it.

All or nothing: either every change passes and there is a diff, or none is accepted and
``validation_errors`` says exactly what is wrong, in words written for the model. The routing
(``after_validate``) then sends the investigation back to the proposer with those errors, at most
``MAX_CHANGE_ATTEMPTS`` attempts in all. An infrastructure problem (the snapshot cannot be made)
is a gap, not a validation error: retrying the model would not help.
"""

import asyncio
import logging
from typing import Any

from langgraph.runtime import Runtime

from app.diff.builder import build_checked_diff
from app.diff.validator import ChangeIssue
from app.diff.validator import validate_changes as validate
from app.graph.code_access import CodeAccessError
from app.graph.state import InvestigationContext, InvestigationState

logger = logging.getLogger(__name__)

_CALLS = 2  # the snapshot, and git apply --check
MAX_ERRORS_SHOWN = 8


def describe_issue(issue: ChangeIssue) -> str:
    """One validation problem as a line the proposer can act on."""
    text = f"change {issue.index} ({issue.file_path}) [{issue.code.value}]: {issue.message}"
    if issue.found_text:
        text += f" The file actually has: {issue.found_text!r}"
    return text


async def validate_changes(state: InvestigationState, runtime: Runtime[InvestigationContext]) -> dict[str, Any]:
    """Fill ``diff`` and ``diff_files`` when everything validates, else ``validation_errors``."""
    changes, repo, incident = state["proposed_changes"], state["repo"], state["incident"]
    ctx = runtime.context
    if not changes or repo is None or incident is None or ctx.code is None:
        return {}

    try:
        async with ctx.code.snapshot(tenant_id=ctx.tenant_id, service_name=incident.service_name, sha=repo.sha) as checkout:
            if checkout is None:
                return {"gaps": ["The proposed changes could not be validated: the repository is no longer connected"], "tool_calls": 1}
            root = checkout.code.root
            report = await asyncio.to_thread(validate, root, changes, read_paths=state["read_paths"])
            if not report.ok:
                errors = [describe_issue(issue) for issue in report.issues[:MAX_ERRORS_SHOWN]]
                return {"validation_errors": errors, "diff": None, "diff_files": [], "tool_calls": _CALLS - 1}
            built = await build_checked_diff(report.edits, root, git_binary=checkout.git.git_binary)
    except CodeAccessError as exc:
        return {"gaps": [f"The proposed changes could not be validated: {exc}"], "validation_errors": [], "tool_calls": 1}
    except Exception:
        logger.warning("validating the changes failed", exc_info=True)
        return {"gaps": ["The proposed changes could not be validated (the code at the pinned commit could not be read)"], "validation_errors": [], "tool_calls": 1}

    if not built.applies:
        return {"validation_errors": [f"the diff built from the changes does not apply to the pinned commit: {built.error}"], "diff": None, "diff_files": [], "tool_calls": _CALLS}
    return {"validation_errors": [], "diff": built.diff, "diff_files": built.files, "tool_calls": _CALLS}
