package com.log0.incident_service.clickhouse;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import javax.sql.DataSource;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Repository;

import lombok.extern.slf4j.Slf4j;

/**
 * Read-only access to the ClickHouse {@code log_events} table.
 *
 * <p>
 * Used by the incident API to retrieve the raw log events that contributed to a
 * given incident. Queries are scoped to both {@code tenant_id} and
 * {@code fingerprint} - the same two columns that form the incident grouping key -
 * so one incident maps directly to one query.
 *
 * <p>
 * Uses plain JDBC with a {@link PreparedStatement}. No ORM - ClickHouse is not a
 * relational database and does not benefit from JPA session management.
 *
 * <p>
 * Results are ordered by {@code timestamp DESC} so the most recent log event
 * appears first, which is the most useful ordering for incident triage.
 *
 * <p>
 * Pagination is handled via SQL {@code LIMIT} / {@code OFFSET}. ClickHouse
 * MergeTree reads are fast even with large offsets because the primary key index
 * (tenant_id, timestamp, fingerprint) prunes irrelevant parts before scanning.
 */
@Slf4j
@Repository
public class LogEventRepository {

    private final DataSource clickHouseDataSource;

    /**
     * @param clickHouseDataSource the ClickHouse DataSource - qualified explicitly
     *                             so Spring does not inject the primary PostgreSQL
     *                             DataSource registered in {@link PostgresDataSourceConfig}
     */
    public LogEventRepository(@Qualifier("clickHouseDataSource") DataSource clickHouseDataSource) {
        this.clickHouseDataSource = clickHouseDataSource;
    }

    private static final String SELECT_SQL = """
            SELECT event_id, tenant_id, service_name, environment, timestamp,
                   level, message, message_template, fingerprint, trace_id, schema_version
            FROM log0.log_events
            WHERE tenant_id = ? AND fingerprint = ?
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """;

    /**
     * Returns a page of log events that share the given {@code fingerprint} within
     * the given tenant, ordered newest-first.
     *
     * <p>
     * The caller is responsible for computing the {@code offset} from the page
     * number ({@code offset = page * size}).
     *
     * <p>
     * {@code traceId} is stored as an empty string when null was inserted - this
     * method converts an empty string back to {@code null} on the way out so that
     * the JSON response omits the field rather than returning an empty string.
     *
     * <p>
     * Any {@link SQLException} is caught and logged; an empty list is returned so
     * that ClickHouse unavailability does not propagate a 500 to the API caller.
     *
     * @param tenantId    the tenant whose logs to query
     * @param fingerprint the error fingerprint shared by all logs for this incident
     * @param limit       maximum number of rows to return
     * @param offset      number of rows to skip (for pagination)
     * @return list of matching log events, newest first; empty list on error
     */
    public List<LogEventDto> findByTenantIdAndFingerprint(UUID tenantId, String fingerprint, int limit, int offset) {
        List<LogEventDto> results = new ArrayList<>();

        try (Connection conn = clickHouseDataSource.getConnection();
                PreparedStatement stmt = conn.prepareStatement(SELECT_SQL)) {

            stmt.setString(1, tenantId.toString());
            stmt.setString(2, fingerprint);
            stmt.setInt(3, limit);
            stmt.setInt(4, offset);

            try (ResultSet rs = stmt.executeQuery()) {
                while (rs.next()) {
                    String traceId = rs.getString("trace_id");

                    results.add(LogEventDto.builder()
                            .eventId(rs.getString("event_id"))
                            .tenantId(rs.getString("tenant_id"))
                            .serviceName(rs.getString("service_name"))
                            .environment(rs.getString("environment"))
                            .timestamp(rs.getTimestamp("timestamp").toInstant())
                            .level(rs.getString("level"))
                            .message(rs.getString("message"))
                            .messageTemplate(rs.getString("message_template"))
                            .fingerprint(rs.getString("fingerprint"))
                            .traceId(traceId != null && !traceId.isEmpty() ? traceId : null)
                            .schemaVersion(rs.getString("schema_version"))
                            .build());
                }
            }

            log.debug("Fetched {} log events for fingerprint {} (tenant {})", results.size(), fingerprint, tenantId);
        } catch (SQLException e) {
            log.error("Failed to query log events for fingerprint {} (tenant {}): {}",
                    fingerprint, tenantId, e.getMessage(), e);
        }

        return results;
    }
}
