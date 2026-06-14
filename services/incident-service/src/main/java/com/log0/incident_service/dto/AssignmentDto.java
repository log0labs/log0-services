package com.log0.incident_service.dto;

import java.time.Instant;
import java.util.UUID;

import com.log0.incident_service.entity.IncidentAssignment;

/**
 * API projection of the current {@link IncidentAssignment} for an incident, returned as part of
 * {@link IncidentDetailResponse}. Mirrors the shape the console expects ({@code assignedToUserId},
 * {@code assignedByUserId}, {@code assignedAt}, {@code notes}).
 */
public record AssignmentDto(
        UUID assignmentId,
        UUID assignedToUserId,
        UUID assignedByUserId,
        Instant assignedAt,
        String notes) {

    public static AssignmentDto from(IncidentAssignment a) {
        return new AssignmentDto(
                a.getAssignmentId(),
                a.getAssignedToUserId(),
                a.getAssignedByUserId(),
                a.getAssignedAt(),
                a.getNotes());
    }
}
