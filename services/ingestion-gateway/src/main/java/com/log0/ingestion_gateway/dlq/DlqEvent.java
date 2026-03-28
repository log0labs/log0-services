package com.log0.ingestion_gateway.dlq;

import java.time.Instant;

import lombok.Builder;
import lombok.Getter;

/**
 * Envelope published to the {@code raw-logs-dlq} topic when ingestion fails.
 *
 * <p>{@code originalEvent} holds the payload that could not be processed,
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
