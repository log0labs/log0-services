from datetime import UTC

from app.schemas.summary import (
    IncidentSummary,
    format_incident_context,
    format_legacy_summary_text,
)


def test_format_legacy_summary_text():
    structured = IncidentSummary(
        summary="Payments are failing.",
        possible_cause="This may be a downstream timeout.",
        recommended_actions=["Check gateway latency", "Review pool settings"],
    )
    text = format_legacy_summary_text(structured)
    assert text.startswith("Summary: Payments are failing.")
    assert "Possible Cause: This may be a downstream timeout." in text
    assert "- Check gateway latency" in text


def test_format_incident_context_includes_service():
    from datetime import datetime
    from uuid import UUID

    from app.schemas.summary import SummaryRequest

    req = SummaryRequest(
        incident_id=UUID("c9d8e7f6-a5b4-4c3d-8e1f-0a9b8c7d6e5f"),
        tenant_id=UUID("b7e1f290-12ab-4cd3-8ef5-6789abcd0123"),
        service_name="payment-service",
        environment="production",
        severity="HIGH",
        occurrence_count=3,
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 2, tzinfo=UTC),
        top_messages=["err"],
    )
    ctx = format_incident_context(req)
    assert "payment-service" in ctx
    assert "HIGH" in ctx
    assert '"err"' in ctx
