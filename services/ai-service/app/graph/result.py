"""What a finished run stores on the investigation row.

The graph state holds snake_case Pydantic objects; the row's JSON columns and the API are
camelCase like everything else the browser sees. This is the one place that converts, so the
rule about what leaves the graph is in one spot: the model's own confidence number
(``model_confidence``) is for logs and calibration and is **never** written to the row.
"""

from typing import Any

from pydantic.alias_generators import to_camel

from app.graph.state import InvestigationState

_PRIVATE = {"model_confidence"}


def _camel(value: Any) -> Any:
    if isinstance(value, dict):
        return {to_camel(k): _camel(v) for k, v in value.items() if k not in _PRIVATE}
    if isinstance(value, list):
        return [_camel(v) for v in value]
    return value


def result_for_row(state: InvestigationState) -> dict[str, Any]:
    """``root_cause``, ``recommendation`` and ``confidence_band`` as the row stores them.
    A key is present only when the run produced it, so a thin run leaves the columns empty."""
    result: dict[str, Any] = {}
    if state.get("root_cause") is not None:
        result["root_cause"] = _camel(state["root_cause"].model_dump())
    if state.get("recommendation") is not None:
        result["recommendation"] = _camel(state["recommendation"].model_dump())
    if state.get("confidence_band") is not None:
        result["confidence_band"] = state["confidence_band"]
    if state.get("repo") is not None:
        result["repo_sha"] = state["repo"].sha  # the exact code the investigation read
    return result
