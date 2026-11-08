"""Assemble the investigation graph.

    START -> load_context -> triage -> (parallel) gather_logs | gather_history |
                                                  gather_code | search_runbooks -> END

``load_context`` ends the run when the incident cannot be read. ``triage`` picks which
gather branches run. Each branch ends the run for now; 2.11 sends them all into
``synthesize``. The graph is pure wiring: every decision lives in a node or in routing.py.
"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from app.graph.nodes.gather_history import gather_history
from app.graph.nodes.gather_logs import gather_logs
from app.graph.nodes.load_context import load_context
from app.graph.nodes.pending import gather_code, search_runbooks
from app.graph.nodes.triage import triage
from app.graph.routing import BRANCH_NODES, after_load_context, fan_out
from app.graph.state import InvestigationContext, InvestigationState

GRAPH_VERSION = "m2-gather-v1"


def build_graph(checkpointer: Checkpointer = None) -> CompiledStateGraph:
    """Compile the graph. Pass a checkpointer to persist state after every step."""
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    
    builder.add_node("load_context", load_context)
    builder.add_node("triage", triage)
    builder.add_node("gather_logs", gather_logs)
    builder.add_node("gather_history", gather_history)
    builder.add_node("gather_code", gather_code)
    builder.add_node("search_runbooks", search_runbooks)
    
    builder.add_edge(START, "load_context")
    builder.add_conditional_edges("load_context", after_load_context, ["triage", END])
    builder.add_conditional_edges("triage", fan_out, list(BRANCH_NODES.values()))
    
    for node in BRANCH_NODES.values():
        builder.add_edge(node, END)

    return builder.compile(checkpointer=checkpointer)
