package com.log0.incident_service.dlq;

import java.time.Instant;

import lombok.Builder;
import lombok.Getter;

/**
 * Envelope published to the {@code incident-events-dlq} topic when incident processing fails.
 *
 * <p>{@code originalEvent} holds the {@code IncidentEvent} that could not be persisted,
 * {@code failedAt} names the service that caught the error, and
 * {@code failedAtTs} records when the failure occurred for lag analysis.
 */
@Getter
@Builder
public class DlqEvent {
    private final Object originalEvent;
    private final String errorMessage;
    private final String failedAt;
    private final Instant failedAtTs;
}