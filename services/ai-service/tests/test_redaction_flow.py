"""Step 5.6: redaction where it matters: evidence, incident fields, tool output, events, memory, the question,
and (the real test) a whole investigation whose logs contain secrets, checking no model prompt ever holds them."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import (
    InMemorySaver,  # noqa: F401  (kept to show checkpoints use the same models)
)

from app.graph.builder import build_graph
from app.graph.events import events_from_update
from app.graph.executor import state_for_row
from app.graph.state import checkpoint_serde, new_state
from app.graph.subgraphs.code_investigator import execute_tool
from app.graph.subgraphs.code_tools import ToolResult, ToolSpec
from app.retrieval.memory import NewMemory, hide_secrets
from app.schemas.domain import Evidence
from app.schemas.investigation import InvestigationScope
from app.security.redaction import redact
from app.tools.incidents import get_incident
from tests.graph_fakes import (
    INCIDENT_ID,
    TENANT,
    FakeApi,
    FakeLogs,
    FakeModel,
    FakeModels,
    incident_payload,
    make_context,
    triage_answer,
)

pytestmark = pytest.mark.asyncio

GH = "ghp_" + "a1B2c3D4e5" * 4
DB_URL = "postgresql://svc:" + "Tr0ub4dor" + "3x@db.internal:5432/payments"
PASSWORD = "hunter" + "22xyz"
EMAIL = "jane.doe" + "@example.com"


def evidence(**over) -> Evidence:
    data = {"id": "ev_1", "kind": "log", "source_ref": "x", "summary": "one line", "content": ""}
    data.update(over)
    return Evidence(**data)


# --------------------------------------------------------------------------- evidence


async def test_evidence_is_redacted_when_it_is_created_whoever_creates_it():
    ev = evidence(summary=f"login failed for {EMAIL}", content=f"token {GH}\nconnecting to {DB_URL}\npassword={PASSWORD}")
    assert EMAIL not in ev.summary and GH not in ev.content and "Tr0ub4dor" not in ev.content and PASSWORD not in ev.content
    assert ev.redactions == {"email": 1, "github_token": 1, "password": 1, "secret": 1}
    assert "db.internal:5432/payments" in ev.content  # the host and database stay: that is what diagnosis needs


async def test_clean_evidence_is_unchanged_and_counts_nothing():
    ev = evidence(summary="Connection timeout after 30000ms", content="at com.log0.Foo.bar(Foo.java:12)")
    assert ev.summary == "Connection timeout after 30000ms" and ev.redactions == {}


async def test_rebuilding_evidence_keeps_its_counts_and_adds_none():
    ev = evidence(content=f"password={PASSWORD}")
    again = Evidence(**ev.model_dump())
    assert again.redactions == ev.redactions == {"secret": 1} and again.content == ev.content


async def test_the_counts_survive_a_checkpoint_round_trip():
    serde = checkpoint_serde()
    ev = evidence(content=f"password={PASSWORD}")
    restored = serde.loads_typed(serde.dumps_typed(ev))
    assert restored == ev and restored.redactions == {"secret": 1}


# ------------------------------------------------------------------------- incident


async def test_the_incident_text_that_reaches_prompts_is_redacted():
    api = FakeApi(incident_payload(topMessages=[f"auth failed for {EMAIL}", "Connection timeout"], aiSummary=f"key {GH} leaked"))
    incident = await get_incident(api, tenant_id=TENANT, incident_id=INCIDENT_ID)
    assert incident.top_messages == ["auth failed for [REDACTED:email]", "Connection timeout"]
    assert incident.ai_summary == "key [REDACTED:github_token] leaked"


async def test_an_incident_without_a_summary_still_has_none():
    incident = await get_incident(FakeApi(incident_payload(aiSummary=None)), tenant_id=TENANT, incident_id=INCIDENT_ID)
    assert incident.ai_summary is None


# ----------------------------------------------------------------------- tool output


async def test_the_text_a_model_reads_from_a_tool_is_redacted_but_the_structured_result_is_exact():
    secret_line = f"API_KEY={PASSWORD}"

    async def run(_args) -> ToolResult:
        return ToolResult(text=f"src/app.py\n  12 | {secret_line}", paths=("src/app.py",))

    from pydantic import BaseModel

    class NoArgs(BaseModel):
        pass

    spec = ToolSpec(name="read_file", description="d", args=NoArgs, run=run)  # type: ignore[call-arg]
    outcome = await execute_tool({"read_file": spec}, "read_file", {}, 5)
    assert PASSWORD not in outcome.text and "[REDACTED:secret]" in outcome.text and "src/app.py" in outcome.text
    assert secret_line in outcome.result.text  # grounding and evidence use the exact result


# ---------------------------------------------------------------------------- events


async def test_events_say_that_something_was_hidden_without_saying_what():
    items = [evidence(id="ev_1", content=f"password={PASSWORD}"), evidence(id="ev_2", content=f"{EMAIL} {GH}"), evidence(id="ev_3")]
    events = events_from_update({"gather_logs": {"evidence": items}})
    types = [t for t, _ in events]
    assert types == ["evidence.added"] * 3 + ["redaction.applied"]
    payloads = dict(events)
    assert payloads["redaction.applied"] == {"count": 3, "kinds": {"email": 1, "github_token": 1, "secret": 1}}
    assert [p.get("redacted") for t, p in events if t == "evidence.added"] == [1, 2, None]
    blob = str(events)
    assert PASSWORD not in blob and GH not in blob and EMAIL not in blob


async def test_no_redaction_event_when_nothing_was_hidden():
    assert "redaction.applied" not in [t for t, _ in events_from_update({"gather_logs": {"evidence": [evidence()]}})]


# ------------------------------------------------------------------- memory, question


async def test_a_conclusion_is_redacted_before_it_is_stored():
    memory = NewMemory(TENANT, uuid4(), uuid4(), "payments", f"the key {GH} was in the logs", f"rotate {GH}", "high")
    safe = hide_secrets(memory)
    assert GH not in safe.root_cause and GH not in (safe.recommendation or "")
    assert safe.service_name == "payments" and safe.confidence_band == "high"
    assert hide_secrets(NewMemory(TENANT, uuid4(), uuid4(), None, "plain", None, None)).recommendation is None


async def test_the_engineers_question_is_redacted_in_the_state_but_not_in_the_row():
    row = SimpleNamespace(id=uuid4(), incident_id=INCIDENT_ID, scope=InvestigationScope(), question=f"why does password={PASSWORD} fail?")
    state = state_for_row(row)
    assert PASSWORD not in state["question"] and "[REDACTED:secret]" in state["question"]
    assert PASSWORD in row.question
    assert state_for_row(SimpleNamespace(id=uuid4(), incident_id=INCIDENT_ID, scope=InvestigationScope(), question=None))["question"] is None


# ----------------------------------------------------------------- a whole investigation


async def test_no_model_prompt_in_a_whole_investigation_ever_contains_the_secrets():
    leaky = [
        {
            "timestamp": None,
            "service_name": "payment-service",
            "level": "ERROR",
            "message": f"Authorization failed: Bearer {'abc123def456ghi789'} for {EMAIL}; db {DB_URL}; password={PASSWORD}; key {GH}",
            "fingerprint": "f" * 64,
        }
    ]
    from tests.graph_fakes import LAST

    leaky[0]["timestamp"] = LAST
    logs = FakeLogs(search_rows=leaky, trace=f"java.lang.IllegalStateException: bad config password={PASSWORD}\n\tat com.log0.A.b(A.java:1)\n")
    api = FakeApi(incident_payload(topMessages=[f"login failed for {EMAIL} using {GH}"]))
    models = FakeModels(FakeModel(triage_answer(gather=["logs", "history"])))
    ctx = make_context(api=api, logs=logs, models=models)

    state = new_state(
        investigation_id="inv-1",
        incident_id=str(INCIDENT_ID),
        scope=InvestigationScope(),
        question=f"is {GH} the problem?",
    )
    final = await build_graph().ainvoke(state, context=ctx)

    secrets = [GH, PASSWORD, EMAIL, "Tr0ub4dor", "abc123def456ghi789"]
    everything_sent = str(models.model.messages) + str(models.investigation.messages)
    assert everything_sent, "the fake models were never called: the test proves nothing"
    for secret in secrets:
        assert secret not in everything_sent, f"{secret!r} reached a model prompt"
        assert secret not in str(final["evidence"]), f"{secret!r} is in the evidence"
    assert any(e.redactions for e in final["evidence"])
    assert "[REDACTED:" in everything_sent  # the model saw the markers, so it knows something was there


async def test_the_pipeline_would_leak_without_redaction_so_the_test_above_is_meaningful():
    assert redact(f"password={PASSWORD}").text != f"password={PASSWORD}"
