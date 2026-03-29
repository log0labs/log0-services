package com.log0.ai_service.dto;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import lombok.Getter;
import lombok.NoArgsConstructor;

/**
 * Request body sent by the Incident Service to {@code POST /api/v1/summaries}.
 *
 * <p>
 * Carries all the incident context the AI service needs to build a prompt
 * and generate a summary - no additional database lookups required on the
 * AI service side. Fields are validated at the controller boundary so that
 * malformed requests are rejected before any LLM call is made.
 */
@Getter
@NoArgsConstructor
public class SummaryRequest {

    /** Unique identifier of the incident this summary is for. */
    @NotNull
    private UUID incidentId;

    /** Tenant that owns this incident - used in the callback URL. */
    @NotBlank
    private String tenantId;

    /**
     * Name of the service that produced the errors (e.g. {@code payment-service}).
     */
    @NotBlank
    private String serviceName;

    /** Deployment environment (e.g. {@code production}, {@code staging}). */
    @NotBlank
    private String environment;

    /**
     * Severity level resolved by the Clustering Service: {@code HIGH},
     * {@code MEDIUM}, or {@code LOW}.
     */
    @NotBlank
    private String severity;

    /** Total number of log occurrences that triggered this incident. */
    @NotNull
    private Long occurrenceCount;

    /** Timestamp of the first matching log event. */
    @NotNull
    private Instant firstSeenAt;

    /** Timestamp of the most recent matching log event. */
    @NotNull
    private Instant lastSeenAt;

    /**
     * Up to 5 distinct error messages from the incident, ordered by frequency.
     * Sourced from the {@code top_messages} column on the {@code incident} row -
     * no ClickHouse query needed for MVP.
     */
    @NotNull
    private List<String> topMessages;
}
