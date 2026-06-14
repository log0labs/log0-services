package com.log0.incident_service.dto;

import java.time.Instant;

import com.log0.incident_service.clickhouse.LogEventDto;

/**
 * API projection of a single raw log event for the incident detail "log samples" panel.
 * Maps the ClickHouse {@code event_id} to {@code logId} (the field name the console expects).
 *
 * <p>
 * {@code trace} is always null: the full stack trace is not persisted to ClickHouse - only the
 * derived {@code trace_id} is - so there is no stack trace to surface here. The field is kept so
 * the response shape matches the console's {@code IncidentLog} type, whose {@code trace} is optional.
 */
public record IncidentLogDto(
        String logId,
        Instant timestamp,
        String level,
        String message,
        String trace) {

    public static IncidentLogDto from(LogEventDto e) {
        return new IncidentLogDto(e.getEventId(), e.getTimestamp(), e.getLevel(), e.getMessage(), null);
    }
}
