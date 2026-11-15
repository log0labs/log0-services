"""Wiring of the remediation graph. Pure wiring and routing: what each node does is in ``nodes.py``.

    START -> preflight -> capture_baseline -> approve_deploy -> trigger_deploy -> wait_deploy -> END
                                                                                   |  ^
                                                                                   v  |
                                                                                   park

A run ends early (END) whenever a node sets ``outcome``: ``refused`` (the offer stands), ``declined`` (a human said no) or
``failed``. After a successful deploy M13 ends the graph at ``wait_deploy``; the soak and the verification (M14) are added
between it and the end.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.deploy.nodes import (
    approve_deploy,
    capture_baseline,
    park,
    preflight,
    trigger_deploy,
    wait_deploy,
)
from app.deploy.state import RemediationContext, RemediationState

GRAPH_VERSION = "v3-deploy-m13"
Checkpointer = BaseCheckpointSaver | None


def after_preflight(state: RemediationState) -> str:
    return END if state.get("outcome") == "refused" else "capture_baseline"


def after_baseline(state: RemediationState) -> str:
    return END if state.get("outcome") == "refused" else "approve_deploy"


def after_approval(state: RemediationState) -> str:
    return "trigger_deploy" if (state.get("approval") or {}).get("action") == "approve" else END


def after_trigger(state: RemediationState) -> str:
    return END if state.get("outcome") == "failed" else "wait_deploy"


def after_wait(state: RemediationState) -> str:
    return "park" if state.get("wake_at") else END


def build_remediation_graph(checkpointer: Checkpointer = None) -> CompiledStateGraph:
    """Compile the graph. Pass a checkpointer to persist state after every step (a run waits for hours)."""
    builder = StateGraph(RemediationState, context_schema=RemediationContext)
    builder.add_node("preflight", preflight)
    builder.add_node("capture_baseline", capture_baseline)
    builder.add_node("approve_deploy", approve_deploy)
    builder.add_node("trigger_deploy", trigger_deploy)
    builder.add_node("wait_deploy", wait_deploy)
    builder.add_node("park", park)

    builder.add_edge(START, "preflight")
    builder.add_conditional_edges("preflight", after_preflight, ["capture_baseline", END])
    builder.add_conditional_edges("capture_baseline", after_baseline, ["approve_deploy", END])
    builder.add_conditional_edges("approve_deploy", after_approval, ["trigger_deploy", END])
    builder.add_conditional_edges("trigger_deploy", after_trigger, ["wait_deploy", END])
    builder.add_conditional_edges("wait_deploy", after_wait, ["park", END])
    builder.add_edge("park", "wait_deploy")
    return builder.compile(checkpointer=checkpointer)
