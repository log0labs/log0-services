package com.log0.incident_service.dto;

import java.time.Instant;
import java.util.UUID;

import com.log0.incident_service.entity.IncidentStateHistory;

/**
 * API projection of one {@link IncidentStateHistory} row for the incident detail timeline.
 * {@code status} is the transition's target state ({@code toStatus}); {@code changedBy} is the
 * acting user (null for system-driven transitions such as the initial {@code NEW}).
 */
public record StatusHistoryDto(
        String status,
        Instant changedAt,
        UUID changedBy) {

    public static StatusHistoryDto from(IncidentStateHistory h) {
        return new StatusHistoryDto(h.getToStatus(), h.getChangedAt(), h.getChangedByUserId());
    }
}
