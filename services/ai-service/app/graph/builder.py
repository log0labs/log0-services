"""Assemble the investigation graph.

    START -> load_context -> triage -> (parallel) gather_logs | gather_history |
                                                  gather_code | search_runbooks -> synthesize -> verify
    verify -> gather_followup -> verify   (while verify wants more log searches and the budget allows)
    verify -> root_cause -> END

``load_context`` ends the run when the incident cannot be read. ``triage`` picks which
gather branches run. Each branch is sent into ``synthesize``. The graph is pure wiring: 
every decision lives in a node or in routing.py.
"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from app.graph.nodes.gather_followup import gather_followup
from app.graph.nodes.gather_history import gather_history
from app.graph.nodes.gather_logs import gather_logs
from app.graph.nodes.load_context import load_context
from app.graph.nodes.pending import gather_code, search_runbooks
from app.graph.nodes.root_cause import root_cause
from app.graph.nodes.synthesize import synthesize
from app.graph.nodes.triage import triage
from app.graph.nodes.verify import verify
from app.graph.routing import BRANCH_NODES, after_load_context, after_verify, fan_out
from app.graph.state import InvestigationContext, InvestigationState

GRAPH_VERSION = "m2-root-cause-v1"


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
    
    builder.add_edge(START, "load_context")
    builder.add_conditional_edges("load_context", after_load_context, ["triage", END])
    builder.add_conditional_edges("triage", fan_out, list(BRANCH_NODES.values()))
    
    for node in BRANCH_NODES.values():
        builder.add_edge(node, "synthesize")
    builder.add_edge("synthesize", "verify")
    builder.add_conditional_edges("verify", after_verify, ["gather_followup", "root_cause"])
    builder.add_edge("root_cause", END)
    builder.add_edge("gather_followup", "verify")

    return builder.compile(checkpointer=checkpointer)
