package com.log0.normalization_service.dlq;

import lombok.Builder;
import lombok.Getter;

import java.time.Instant;

/**
 * Envelope published to the {@code raw-logs-dlq} topic when normalization fails.
 *
 * <p>{@code originalEvent} holds the raw payload that could not be normalized,
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
