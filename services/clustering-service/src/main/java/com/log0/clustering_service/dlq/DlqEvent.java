package com.log0.clustering_service.dlq;

import java.time.Instant;

import lombok.Builder;
import lombok.Getter;

/**
 * Envelope published to the {@code raw-logs-dlq} topic when clustering fails.
 *
 * <p>{@code originalEvent} holds the normalized log that could not be clustered,
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
