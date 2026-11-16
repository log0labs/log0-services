"""Wiring of the remediation graph. Pure wiring and routing: what each node does is in ``nodes.py``.

    START -> preflight -> capture_baseline -> approve_deploy -> trigger_deploy -> wait_deploy -> END
                                                                                   |  ^
                                                                                   v  |
                                                                                   park

A run ends early (END) whenever a node sets ``outcome``: ``refused`` (the offer stands), ``declined`` (a human said no) or
``failed``. After a successful deploy comes the soak and the verdict (``soak``, ``verify``, ``decide_verdict``). A person who
rolls back sends the run through a second, shorter copy of the deploy path (``trigger_rollback``, ``wait_rollback``,
``soak_rollback``, ``verify_rollback``) that always ends: one rollback at most, never another verdict gate.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.deploy.nodes import (
    approve_deploy,
    capture_baseline,
    decide_verdict,
    park,
    preflight,
    soak,
    soak_rollback,
    trigger_deploy,
    trigger_rollback,
    verify,
    verify_rollback,
    wait_deploy,
    wait_rollback,
)
from app.deploy.state import RemediationContext, RemediationState

GRAPH_VERSION = "v3-deploy-m15"
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
    if state.get("wake_at"):
        return "park"
    return "soak" if state.get("outcome") == "deployed" else END


def after_soak(state: RemediationState) -> str:
    if state.get("judgement"):
        return "verify"
    return "park" if state.get("wake_at") else END


def after_park(state: RemediationState) -> str:
    """A timer ran out: back to whatever was waiting. The rollback's legs come first (``outcome == "rolled_back"`` is its soak,
    ``phase == "rollback"`` its pipeline wait); otherwise a successful deploy (``outcome == "deployed"``) is the soak."""
    if state.get("outcome") == "rolled_back":
        return "soak_rollback"
    if state.get("phase") == "rollback":
        return "wait_rollback"
    return "soak" if state.get("outcome") == "deployed" else "wait_deploy"


def after_verify(state: RemediationState) -> str:
    return END if state.get("outcome") == "verified" else "decide_verdict"


def after_decision(state: RemediationState) -> str:
    decision = state.get("decision")
    return "trigger_rollback" if decision == "rollback" else "soak" if decision == "extend" else END


def after_trigger_rollback(state: RemediationState) -> str:
    return END if state.get("outcome") == "failed" else "wait_rollback"


def after_wait_rollback(state: RemediationState) -> str:
    if state.get("wake_at"):
        return "park"
    return "soak_rollback" if state.get("outcome") == "rolled_back" else END


def after_soak_rollback(state: RemediationState) -> str:
    if state.get("recovery"):
        return "verify_rollback"
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
    builder.add_node("soak", soak)
    builder.add_node("verify", verify)
    builder.add_node("decide_verdict", decide_verdict)
    builder.add_node("trigger_rollback", trigger_rollback)
    builder.add_node("wait_rollback", wait_rollback)
    builder.add_node("soak_rollback", soak_rollback)
    builder.add_node("verify_rollback", verify_rollback)

    builder.add_edge(START, "preflight")
    builder.add_conditional_edges("preflight", after_preflight, ["capture_baseline", END])
    builder.add_conditional_edges("capture_baseline", after_baseline, ["approve_deploy", END])
    builder.add_conditional_edges("approve_deploy", after_approval, ["trigger_deploy", END])
    builder.add_conditional_edges("trigger_deploy", after_trigger, ["wait_deploy", END])
    builder.add_conditional_edges("wait_deploy", after_wait, ["park", "soak", END])
    builder.add_conditional_edges("soak", after_soak, ["verify", "park", END])
    builder.add_conditional_edges("park", after_park, ["soak", "wait_deploy", "soak_rollback", "wait_rollback"])
    builder.add_conditional_edges("verify", after_verify, ["decide_verdict", END])
    builder.add_conditional_edges("decide_verdict", after_decision, ["soak", "trigger_rollback", END])
    builder.add_conditional_edges("trigger_rollback", after_trigger_rollback, ["wait_rollback", END])
    builder.add_conditional_edges("wait_rollback", after_wait_rollback, ["park", "soak_rollback", END])
    builder.add_conditional_edges("soak_rollback", after_soak_rollback, ["verify_rollback", "park", END])
    builder.add_edge("verify_rollback", END)
    return builder.compile(checkpointer=checkpointer)
