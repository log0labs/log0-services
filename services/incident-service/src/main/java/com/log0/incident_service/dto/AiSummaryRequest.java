package com.log0.incident_service.dto;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

/**
 * Request body sent to the AI Summary Service when requesting a generated
 * incident summary.
 *
 * <p>
 * Field names and types must match the {@code SummaryRequest} DTO on the
 * ai-service side. Implemented as a {@code record} so Jackson serializes a
 * non-empty JSON body when posted via {@code RestClient}.
 */
public record AiSummaryRequest(
        UUID incidentId,
        String tenantId,
        String serviceName,
        String environment,
        String severity,
        Long occurrenceCount,
        Instant firstSeenAt,
        Instant lastSeenAt,
        List<String> topMessages) {
}
