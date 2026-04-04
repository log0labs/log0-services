package com.log0.incident_service.clickhouse;

import java.time.Instant;

import lombok.Builder;
import lombok.Value;

/**
 * Read-only projection of a single row from the ClickHouse {@code log_events} table.
 *
 * <p>
 * Returned by {@link LogEventRepository#findByTenantIdAndFingerprint} and
 * surfaced through {@code GET /api/v1/incidents/{id}/logs}. This is a pure
 * data carrier - no JPA, no ORM.
 */
@Value
@Builder
public class LogEventDto {

    String eventId;
    String tenantId;
    String serviceName;
    String environment;
    Instant timestamp;
    String level;
    String message;
    String messageTemplate;
    String fingerprint;
    String traceId;
    String schemaVersion;
}
