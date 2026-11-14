"""Placeholders for gather branches that arrive in later milestones.

Triage may ask for ``runbooks`` today, and an engineer who approves the root cause expects a proposed
fix (``propose_changes``, step 4.2). Rather than silently ignoring the request (the report
would then look complete when it is not), the placeholder records a gap. When the real branch
exists (runbooks in M5) it replaces the placeholder in ``graph/builder.py``; nothing else changes.
(The code branch became real in M3: ``nodes/gather_code.py``.)
"""

from typing import Any

from langgraph.runtime import Runtime

from app.graph.state import InvestigationContext, InvestigationState


async def propose_changes(
    state: InvestigationState, runtime: Runtime[InvestigationContext]
) -> dict[str, Any]:
    return {"gaps": ["No fix was proposed: proposing code changes is not available in this version"]}


async def search_runbooks(
    state: InvestigationState, runtime: Runtime[InvestigationContext]
) -> dict[str, Any]:
    return {"gaps": ["Runbooks and docs were not searched: that search is not available in this version"]}
