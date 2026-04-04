package com.log0.normalization_service.clickhouse;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Timestamp;

import javax.sql.DataSource;

import org.springframework.stereotype.Repository;

import com.log0.normalization_service.dto.NormalizedLogEvent;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Persists {@link NormalizedLogEvent} records into the {@code log_events} table
 * in ClickHouse.
 *
 * <p>
 * Uses plain JDBC with a {@link PreparedStatement} - no ORM. ClickHouse's
 * MergeTree
 * engine handles ordering and compaction; this repository only appends rows.
 *
 * <p>
 * Write failures are intentionally caught and logged here rather than rethrown.
 * ClickHouse is append-only analytics storage - a write failure must not affect
 * the Kafka offset acknowledgment or trigger the DLQ flow in
 * {@code RawLogConsumer}.
 */
@Slf4j
@Repository
@RequiredArgsConstructor
public class LogEventRepository {
    // Injected by Spring - the same ClickHouseDataSource bean created in
    // ClickHouseConfig
    private final DataSource clickHouseDataSource;

    // Parameterised INSERT - the ? placeholders are filled by
    // PreparedStatement.setString()
    // which prevents SQL injection and handles escaping automatically
    private static final String INSERT_SQL = """
            INSERT INTO log_events
                (event_id, tenant_id, service_name, environment, timestamp,
                 level, message, message_template, fingerprint,
                 trace_id, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """;

    /**
     * Inserts a single normalized log event into ClickHouse.
     *
     * <p>
     * Opens a connection, prepares the INSERT statement, binds all fields from
     * {@code event}, and executes. The connection is closed automatically by the
     * try-with-resources block regardless of success or failure.
     *
     * <p>
     * {@code traceId} is nullable on {@link NormalizedLogEvent} - a null value is
     * stored as an empty string rather than SQL NULL because ClickHouse's
     * {@code String}
     * type does not have a nullable variant in this schema.
     *
     * <p>
     * Any {@link SQLException} is caught, logged, and swallowed - the caller
     * ({@code RawLogConsumer}) must still acknowledge the Kafka offset even if
     * ClickHouse is temporarily unavailable.
     *
     * @param event the normalized log event to persist; must not be null
     */
    public void save(NormalizedLogEvent event) {
        try (Connection conn = clickHouseDataSource.getConnection();
                PreparedStatement stmt = conn.prepareStatement(INSERT_SQL)) {
            // Bind each field in the same order as the INSERT column list above
            stmt.setString(1, event.getEventId());
            stmt.setString(2, event.getTenantId());
            stmt.setString(3, event.getServiceName());
            stmt.setString(4, event.getEnvironment());

            // Use Timestamp.from(Instant) instead of setString(Instant.toString()).
            // The ClickHouse JDBC driver internally calls LocalDateTime.parse() on string
            // values, which rejects ISO-8601's 'T' separator and 'Z' suffix and throws a
            // DateTimeParseException. java.sql.Timestamp maps directly to DateTime64.
            stmt.setTimestamp(5, Timestamp.from(event.getTimestamp()));

            stmt.setString(6, event.getLevel());
            stmt.setString(7, event.getMessage());
            stmt.setString(8, event.getMessageTemplate());
            stmt.setString(9, event.getFingerprint());

            // traceId is optional - fall back to empty string to avoid NULL in ClickHouse
            // String column
            stmt.setString(10, event.getTraceId() != null ? event.getTraceId() : "");

            stmt.setString(11, event.getSchemaVersion());

            stmt.executeUpdate();
            log.debug("Saved log event {} to ClickHouse", event.getEventId());
        } catch (SQLException e) {
            // Log but do not rethrow - ClickHouse unavailability must not block the Kafka
            // pipeline
            log.error("Failed to save log event {} to ClickHouse: {}",
                    event.getEventId(), e.getMessage(), e);
        }
    }
}
