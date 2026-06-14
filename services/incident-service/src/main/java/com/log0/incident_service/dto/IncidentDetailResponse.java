package com.log0.incident_service.dto;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import com.log0.incident_service.entity.Incident;

/**
 * Full incident detail returned by {@code GET /api/v1/incidents/{id}}.
 *
 * <p>
 * Extends the core incident fields with the current {@link AssignmentDto} and the ordered
 * {@link StatusHistoryDto} timeline, joining {@code incident_assignment} and
 * {@code incident_state_history} so the console can render the assignee, the state-history
 * timeline, and gate engineer actions (acknowledge/resolve) on {@code assignment.assignedToUserId}.
 * The bare {@link Incident} entity carried none of this, so those features were previously inert.
 */
public record IncidentDetailResponse(
        UUID incidentId,
        UUID tenantId,
        String fingerprint,
        String serviceName,
        String environment,
        String severity,
        String status,
        Long occurrenceCount,
        Instant firstSeenAt,
        Instant lastSeenAt,
        Instant resolvedAt,
        List<String> topMessages,
        String aiSummary,
        Instant createdAt,
        Instant updatedAt,
        AssignmentDto assignment,
        List<StatusHistoryDto> statusHistory) {

    public static IncidentDetailResponse from(
            Incident i, AssignmentDto assignment, List<StatusHistoryDto> statusHistory) {
        return new IncidentDetailResponse(
                i.getIncidentId(),
                i.getTenantId(),
                i.getFingerprint(),
                i.getServiceName(),
                i.getEnvironment(),
                i.getSeverity(),
                i.getStatus(),
                i.getOccurrenceCount(),
                i.getFirstSeenAt(),
                i.getLastSeenAt(),
                i.getResolvedAt(),
                i.getTopMessages(),
                i.getAiSummary(),
                i.getCreatedAt(),
                i.getUpdatedAt(),
                assignment,
                statusHistory);
    }
}
