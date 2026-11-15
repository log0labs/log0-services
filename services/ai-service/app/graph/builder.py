"""Assemble the investigation graph.

    START -> load_context -> triage -> (parallel) gather_logs | gather_history |
                                                  gather_code | search_runbooks -> synthesize -> verify
    verify -> gather_followup -> verify   (while verify wants more log searches and the budget allows)
    verify -> root_cause -> human_review -> END | synthesize (refine) | propose_changes
    propose_changes -> validate_changes -> propose_changes (retry, max 2 attempts) | offer_pr | finalize
    offer_pr -> approve_pr | finalize;  approve_pr -> create_branch -> apply_patch -> open_draft_pr -> finalize -> END   (V2: only after an engineer approves;
                  any step that cannot write goes to finalize with a gap)

``load_context`` ends the run when the incident cannot be read. ``triage`` picks which
gather branches run. Each branch is sent into ``synthesize``. The graph is pure wiring: 
every decision lives in a node or in routing.py.
"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from app.graph.nodes.finalize import finalize
from app.graph.nodes.gather_code import gather_code
from app.graph.nodes.gather_followup import gather_followup
from app.graph.nodes.gather_history import gather_history
from app.graph.nodes.gather_logs import gather_logs
from app.graph.nodes.human_review import human_review
from app.graph.nodes.load_context import load_context
from app.graph.nodes.propose_changes import propose_changes
from app.graph.nodes.pull_request import (
    apply_patch,
    approve_pr,
    create_branch,
    offer_pr,
    open_draft_pr,
)
from app.graph.nodes.root_cause import root_cause
from app.graph.nodes.search_runbooks import search_runbooks
from app.graph.nodes.synthesize import synthesize
from app.graph.nodes.triage import triage
from app.graph.nodes.validate_changes import validate_changes
from app.graph.nodes.verify import verify
from app.graph.routing import (
    BRANCH_NODES,
    after_apply_patch,
    after_create_branch,
    after_load_context,
    after_offer,
    after_pr_approval,
    after_propose,
    after_review,
    after_validate,
    after_verify,
    fan_out,
)
from app.graph.state import InvestigationContext, InvestigationState

GRAPH_VERSION = "v2-draft-pr-v1"


def build_graph(checkpointer: Checkpointer = None) -> CompiledStateGraph:
    """Compile the graph. Pass a checkpointer to persist state after every step."""
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    
    builder.add_node("load_context", load_context)
    builder.add_node("triage", triage)
    builder.add_node("gather_logs", gather_logs)
    builder.add_node("gather_history", gather_history)
    builder.add_node("gather_code", gather_code)
    builder.add_node("search_runbooks", search_runbooks)
    builder.add_node("synthesize", synthesize)
    builder.add_node("verify", verify)
    builder.add_node("root_cause", root_cause)
    builder.add_node("gather_followup", gather_followup)
    builder.add_node("human_review", human_review)
    builder.add_node("propose_changes", propose_changes)
    builder.add_node("validate_changes", validate_changes)
    builder.add_node("offer_pr", offer_pr)
    builder.add_node("approve_pr", approve_pr)
    builder.add_node("create_branch", create_branch)
    builder.add_node("apply_patch", apply_patch)
    builder.add_node("open_draft_pr", open_draft_pr)
    builder.add_node("finalize", finalize)
    
    builder.add_edge(START, "load_context")
    builder.add_conditional_edges("load_context", after_load_context, ["triage", END])
    builder.add_conditional_edges("triage", fan_out, list(BRANCH_NODES.values()))
    
    for node in BRANCH_NODES.values():
        builder.add_edge(node, "synthesize")
    builder.add_edge("synthesize", "verify")
    builder.add_conditional_edges("verify", after_verify, ["gather_followup", "root_cause"])
    builder.add_edge("root_cause", "human_review")
    builder.add_conditional_edges("human_review", after_review, ["synthesize", "propose_changes", END])
    builder.add_conditional_edges("propose_changes", after_propose, ["validate_changes", "finalize"])
    builder.add_conditional_edges("validate_changes", after_validate, ["propose_changes", "offer_pr", "finalize"])
    builder.add_conditional_edges("offer_pr", after_offer, ["approve_pr", "finalize"])
    builder.add_conditional_edges("approve_pr", after_pr_approval, ["create_branch", "finalize"])
    builder.add_conditional_edges("create_branch", after_create_branch, ["apply_patch", "finalize"])
    builder.add_conditional_edges("apply_patch", after_apply_patch, ["open_draft_pr", "finalize"])
    builder.add_edge("open_draft_pr", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("gather_followup", "verify")

    return builder.compile(checkpointer=checkpointer)
