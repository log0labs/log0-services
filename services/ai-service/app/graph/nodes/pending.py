"""Placeholders for gather branches that arrive in later milestones.

Triage may ask for ``code`` or ``runbooks`` today. Rather than silently ignoring the request
(the report would then look complete when it is not), each placeholder records a gap. When
the real branch exists (code in M3, runbooks in M5) it replaces the placeholder in
``graph/builder.py``; nothing else changes.
"""

from typing import Any

from langgraph.runtime import Runtime

from app.graph.state import InvestigationContext, InvestigationState


async def gather_code(
    state: InvestigationState, runtime: Runtime[InvestigationContext]
) -> dict[str, Any]:
    return {"gaps": ["The code was not searched: code investigation is not available in this version"]}


async def search_runbooks(
    state: InvestigationState, runtime: Runtime[InvestigationContext]
) -> dict[str, Any]:
    return {"gaps": ["Runbooks and docs were not searched: that search is not available in this version"]}
