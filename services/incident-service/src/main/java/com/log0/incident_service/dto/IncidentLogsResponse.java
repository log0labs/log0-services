package com.log0.incident_service.dto;

import java.util.List;

/**
 * Paged response for {@code GET /api/v1/incidents/{id}/logs}.
 *
 * <p>
 * Wraps the log rows in the {@code content}/{@code totalElements}/{@code totalPages}/{@code number}
 * shape the console expects, instead of the previous bare {@code List}. {@code totalElements} comes
 * from a ClickHouse {@code count()} on the incident's fingerprint.
 */
public record IncidentLogsResponse(
        List<IncidentLogDto> content,
        long totalElements,
        int totalPages,
        int number) {
}
