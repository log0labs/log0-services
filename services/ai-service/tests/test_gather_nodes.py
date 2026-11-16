"""Unit tests for gather_logs and gather_history (no network)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import OperationalError

from app.graph.nodes.gather_history import gather_history
from app.graph.nodes.gather_logs import MAX_SLOTS, gather_logs, pick_bucket
from app.graph.nodes.load_context import incident_evidence
from app.graph.state import InvestigationContext, InvestigationState, new_state
from app.integrations.clickhouse import ClickHouseError
from app.schemas.investigation import InvestigationScope
from app.tools.incidents import IncidentContext
from tests.graph_fakes import (
    FINGERPRINT,
    FIRST,
    INCIDENT_ID,
    LAST,
    OTHER_TENANT,
    TENANT,
    FakeHistory,
    FakeLogs,
    history_row,
    make_context,
)


def _state(*, with_incident: bool = True) -> InvestigationState:
    state = new_state(investigation_id="i1", incident_id=str(INCIDENT_ID), scope=InvestigationScope())
    if with_incident:
        incident = IncidentContext(
            incident_id=INCIDENT_ID,
            fingerprint=FINGERPRINT,
            service_name="payment-service",
            environment="production",
            severity="HIGH",
            status="NEW",
            occurrence_count=1500,
            first_seen_at=FIRST,
            last_seen_at=LAST,
            resolved_at=None,
            top_messages=["Connection timeout after 30000ms"],
            ai_summary=None,
        )
        state["incident"] = incident
        state["evidence"] = [incident_evidence(incident)]
    return state


async def _run(node: Any, state: InvestigationState, ctx: InvestigationContext) -> InvestigationState:
    builder = StateGraph(InvestigationState, context_schema=InvestigationContext)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return await builder.compile().ainvoke(state, context=ctx)


# ------------------------------------------------------------------- gather_logs


@pytest.mark.asyncio
async def test_gather_logs_adds_timeline_neighbours_and_samples() -> None:
    final = await _run(gather_logs, _state(), make_context())
    by_kind = {e.source_ref.split()[0]: e for e in final["evidence"]}

    timeline = by_kind["timeline"]
    assert timeline.kind == "timeline"
    assert "40 occurrences in 2 of" in timeline.summary and "peak 30 at 2026-01-01 12:01" in timeline.summary
    assert "2026-01-01 12:01  30" in timeline.content

    neighbours = by_kind["cooccurring"]
    assert "1 other failing pattern(s)" in neighbours.summary
    assert "payment-gateway-client: upstream returned 503" in neighbours.summary
    assert "(12x, 12:00-12:10)" in neighbours.content

    samples = by_kind["samples"]
    assert "1 most recent ERROR lines from payment-service" in samples.summary
    assert "[this incident] Connection timeout after 30000ms" in samples.content

    assert final["gaps"] == []
    assert final["tool_calls"] == 3


@pytest.mark.asyncio
async def test_every_log_query_uses_the_context_tenant_and_the_same_window() -> None:
    logs = FakeLogs()
    await _run(gather_logs, _state(), make_context(logs=logs))
    assert [k for k, _, _ in logs.calls].count("timeline") == 1
    assert sorted(k for k, _, _ in logs.calls) == ["cooccurring", "search", "timeline"]
    assert logs.tenants_used() == {TENANT}
    windows = {(p["start"], p["end"]) for k, _, p in logs.calls if k != "timeline"}
    assert len(windows) == 1  # co-occurring and search ask about the very same window
    search = next(p for k, _, p in logs.calls if k == "search")
    assert search["service"] == "payment-service" and search["level"] == "ERROR"
    neighbours = next(p for k, _, p in logs.calls if k == "cooccurring")
    assert neighbours["exclude_fingerprint"] == FINGERPRINT  # "what ELSE was failing"


@pytest.mark.asyncio
async def test_no_neighbours_is_evidence_not_a_gap() -> None:
    final = await _run(gather_logs, _state(), make_context(logs=FakeLogs(cooccurring_rows=[])))
    neighbours = next(e for e in final["evidence"] if e.source_ref.startswith("cooccurring"))
    assert neighbours.summary.startswith("No other WARN/ERROR/FATAL patterns were logged by any service")
    assert final["gaps"] == []  # an isolated failure is a finding, not a hole in the data


@pytest.mark.asyncio
async def test_an_empty_timeline_adds_no_evidence_and_no_duplicate_gap() -> None:
    final = await _run(gather_logs, _state(), make_context(logs=FakeLogs(timeline_rows=[])))
    assert not any(e.source_ref.startswith("timeline") for e in final["evidence"])
    assert final["gaps"] == []  # load_context already reports "no log events found"


@pytest.mark.asyncio
async def test_no_error_lines_is_evidence() -> None:
    final = await _run(gather_logs, _state(), make_context(logs=FakeLogs(search_rows=[])))
    samples = next(e for e in final["evidence"] if e.source_ref.startswith("samples"))
    assert samples.summary.startswith("payment-service logged no ERROR lines")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing", "gap_text", "kept"),
    [
        ("timeline", "timeline could not be read", {"cooccurring", "samples"}),
        ("cooccurring", "co-occurring errors could not be read", {"timeline", "samples"}),
        ("search", "sample log lines could not be read", {"timeline", "cooccurring"}),
    ],
)
async def test_one_failing_query_becomes_a_gap_and_keeps_the_others(
    failing: str, gap_text: str, kept: set[str]
) -> None:
    logs = FakeLogs(errors={failing: ClickHouseError("down")})
    final = await _run(gather_logs, _state(), make_context(logs=logs))
    assert len(final["gaps"]) == 1 and gap_text in final["gaps"][0]
    assert {e.source_ref.split()[0] for e in final["evidence"] if not e.source_ref.startswith("incident")} == kept
    assert final["tool_calls"] == 3  # the attempts still count against the budget


@pytest.mark.asyncio
async def test_a_programming_error_is_not_swallowed() -> None:
    logs = FakeLogs(errors={"search": KeyError("boom")})
    with pytest.raises(KeyError):
        await _run(gather_logs, _state(), make_context(logs=logs))


@pytest.mark.asyncio
async def test_gather_logs_without_an_incident_does_nothing() -> None:
    logs = FakeLogs()
    final = await _run(gather_logs, _state(with_incident=False), make_context(logs=logs))
    assert final["evidence"] == [] and logs.calls == []


def test_bucket_grows_with_the_window_and_stays_inside_the_timeline_limit() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def bucket(minutes: int) -> str:
        return pick_bucket(start, start + timedelta(minutes=minutes))

    assert bucket(60) == "1m" and bucket(119) == "1m"  # up to 119 one-minute slots + 1
    assert bucket(120) == "5m" and bucket(9 * 60) == "5m"
    assert bucket(10 * 60) == "15m" and bucket(29 * 60) == "15m"
    assert bucket(30 * 60) == "1h" and bucket(7 * 24 * 60) == "1h"  # 1h is the last resort
    # the finest choice always fits MAX_SLOTS, except the 1h fallback (<= 169 slots, under the tool's 500)
    for minutes in (30, 600, 1000, 1700):
        step = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}[bucket(minutes)]
        assert minutes * 60 / step + 1 <= MAX_SLOTS


# ----------------------------------------------------------------- gather_history


@pytest.mark.asyncio
async def test_each_earlier_incident_becomes_evidence() -> None:
    other = history_row(
        incident_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        same_fingerprint=False,
        service_name="payment-service",
        resolved_at=None,
        status="ACKNOWLEDGED",
        ai_summary=None,
    )
    history = FakeHistory([history_row(), other])
    final = await _run(gather_history, _state(), make_context(history=history))
    summaries = [e.summary for e in final["evidence"] if e.kind == "incident" and "Earlier incident" in e.summary]
    assert summaries[0] == (
        "Earlier incident, same error: HIGH, 120 occurrences, 2025-12-20 to 2025-12-20, resolved 2025-12-20"
    )
    assert "same service (payment-service)" in summaries[1] and "status ACKNOWLEDGED" in summaries[1]
    first = next(e for e in final["evidence"] if "same error" in e.summary)
    assert "Gateway timeout raised from 5s to 30s" in first.content  # the earlier fix is the useful part
    assert final["tool_calls"] == 1 and final["gaps"] == []


@pytest.mark.asyncio
async def test_history_is_queried_for_this_tenant_and_incident() -> None:
    history = FakeHistory()
    await _run(gather_history, _state(), make_context(history=history, tenant=OTHER_TENANT))
    call = history.calls[0]
    assert call["tenant_id"] == OTHER_TENANT  # from the context, never from the model or state
    assert call["incident_id"] == INCIDENT_ID and call["fingerprint"] == FINGERPRINT
    assert call["limit"] == 8


@pytest.mark.asyncio
async def test_no_history_is_evidence_not_a_gap() -> None:
    final = await _run(gather_history, _state(), make_context(history=FakeHistory([])))
    added = [e for e in final["evidence"] if e.source_ref.startswith("history")]
    assert len(added) == 1 and added[0].summary.startswith("No earlier incident with this fingerprint")
    assert final["gaps"] == []


@pytest.mark.asyncio
async def test_a_database_outage_becomes_a_gap() -> None:
    history = FakeHistory(error=OperationalError("select", {}, Exception("connection refused")))
    final = await _run(gather_history, _state(), make_context(history=history))
    assert final["gaps"] == ["Earlier incidents could not be read (the incident database is unavailable)"]
    assert [e.kind for e in final["evidence"]] == ["incident"]  # only load_context's own evidence
    assert final["tool_calls"] == 1


@pytest.mark.asyncio
async def test_a_missing_password_is_not_hidden_as_a_gap() -> None:
    history = FakeHistory(error=RuntimeError("INCIDENT_DB_PASSWORD is not set"))
    with pytest.raises(RuntimeError, match="INCIDENT_DB_PASSWORD"):
        await _run(gather_history, _state(), make_context(history=history))


@pytest.mark.asyncio
async def test_gather_history_without_an_incident_does_nothing() -> None:
    history = FakeHistory()
    final = await _run(gather_history, _state(with_incident=False), make_context(history=history))
    assert final["evidence"] == [] and history.calls == []
