package com.log0.incident_service.dto;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import lombok.Builder;
import lombok.Getter;

/**
 * Request body sent to the AI Summary Service when requesting a generated
 * incident summary.
 *
 * <p>
 * Field names and types must match the {@code SummaryRequest} DTO on the
 * ai-service side so that Jackson can serialize this object into the expected
 * JSON shape. Built via the Lombok {@code @Builder} pattern from an
 * {@link com.log0.incident_service.entity.Incident} entity.
 */
@Getter
@Builder
public class AiSummaryRequest {

    /** The unique identifier of the incident being summarised. */
    private UUID incidentId;

    /** The tenant that owns this incident (string form of the UUID). */
    private String tenantId;

    /** The service that produced the incident (e.g. {@code payment-service}). */
    private String serviceName;

    /** The deployment environment (e.g. {@code production}). */
    private String environment;

    /** The incident severity level (e.g. {@code HIGH}, {@code CRITICAL}). */
    private String severity;

    /** Total number of times the error has been observed. */
    private Long occurrenceCount;

    /** Timestamp of the first error occurrence for this incident. */
    private Instant firstSeenAt;

    /** Timestamp of the most recent error occurrence. */
    private Instant lastSeenAt;

    /** Representative error messages sampled from this incident's error events. */
    private List<String> topMessages;
}
