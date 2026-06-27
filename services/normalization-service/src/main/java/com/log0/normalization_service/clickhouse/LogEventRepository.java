package com.log0.normalization_service.clickhouse;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.locks.ReentrantLock;

import javax.sql.DataSource;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Repository;

import com.log0.normalization_service.dto.NormalizedLogEvent;

import jakarta.annotation.PreDestroy;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Persists {@link NormalizedLogEvent} records into the {@code log_events} table
 * in ClickHouse, using buffered batch inserts.
 *
 * <p>
 * ClickHouse is a columnar OLAP store: it is built for a few large inserts, not
 * many tiny ones. A single-row INSERT per event pegs the server and caps
 * throughput at a few dozen rows/second. So {@link #save} only enqueues; rows
 * are written in batches when the buffer fills ({@code clickhouse.batch-size})
 * or on a fixed interval ({@code clickhouse.flush-interval-ms}), whichever comes
 * first, via a single {@link PreparedStatement#executeBatch()}.
 *
 * <p>
 * Durability note: buffered rows live in memory until the next flush, and any
 * remaining buffer is flushed on shutdown ({@link #shutdownFlush}). ClickHouse
 * here is append-only analytics storage, not the system of record - the durable
 * source is the {@code raw-logs} topic in Redpanda, which is replayable. So a
 * dropped buffer on a hard crash is recoverable by replay and must never block
 * the Kafka pipeline.
 */
@Slf4j
@Repository
@RequiredArgsConstructor
public class LogEventRepository {
    private final DataSource clickHouseDataSource;

    private static final String INSERT_SQL = """
            INSERT INTO log_events
                (event_id, tenant_id, service_name, environment, timestamp,
                 level, message, message_template, fingerprint,
                 trace_id, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """;

    @Value("${clickhouse.batch-size:500}")
    private int batchSize;

    private final ConcurrentLinkedQueue<NormalizedLogEvent> buffer = new ConcurrentLinkedQueue<>();
    private final ReentrantLock flushLock = new ReentrantLock();

    /**
     * Enqueues an event for the next batch. Triggers an immediate flush once the
     * buffer reaches the configured batch size so high-throughput bursts do not
     * accumulate unbounded memory.
     */
    public void save(NormalizedLogEvent event) {
        buffer.add(event);
        if (buffer.size() >= batchSize) {
            flush();
        }
    }

    /**
     * Drains the buffer and writes it to ClickHouse as one batched INSERT. Runs on
     * a fixed interval (so partial batches still land promptly under low load) and
     * is also called inline by {@link #save} when the buffer is full. Only one
     * flush runs at a time; concurrent callers return immediately.
     */
    @Scheduled(fixedDelayString = "${clickhouse.flush-interval-ms:1000}")
    public void flush() {
        if (buffer.isEmpty() || !flushLock.tryLock()) {
            return;
        }
        try {
            // Cap a single flush so one statement stays bounded even after a big burst.
            int max = batchSize * 4;
            List<NormalizedLogEvent> batch = new ArrayList<>(Math.min(max, buffer.size()));
            NormalizedLogEvent e;
            while (batch.size() < max && (e = buffer.poll()) != null) {
                batch.add(e);
            }
            if (batch.isEmpty()) {
                return;
            }
            try (Connection conn = clickHouseDataSource.getConnection();
                    PreparedStatement stmt = conn.prepareStatement(INSERT_SQL)) {
                for (NormalizedLogEvent ev : batch) {
                    stmt.setString(1, ev.getEventId());
                    stmt.setString(2, ev.getTenantId());
                    stmt.setString(3, ev.getServiceName());
                    stmt.setString(4, ev.getEnvironment());
                    stmt.setTimestamp(5, Timestamp.from(ev.getTimestamp()));
                    stmt.setString(6, ev.getLevel());
                    stmt.setString(7, ev.getMessage());
                    stmt.setString(8, ev.getMessageTemplate());
                    stmt.setString(9, ev.getFingerprint());
                    stmt.setString(10, ev.getTraceId() != null ? ev.getTraceId() : "");
                    stmt.setString(11, ev.getSchemaVersion());
                    stmt.addBatch();
                }
                stmt.executeBatch();
                log.debug("Flushed {} log events to ClickHouse", batch.size());
            } catch (SQLException ex) {
                // Drop this batch (do not requeue - avoids unbounded growth if ClickHouse is
                // down). Rows remain replayable from raw-logs. Never blocks the pipeline.
                log.error("Failed to flush {} log events to ClickHouse: {}", batch.size(), ex.getMessage(), ex);
            }
        } finally {
            flushLock.unlock();
        }
    }

    /**
     * Flushes any remaining buffered rows on graceful shutdown so a normal stop
     * does not lose the last partial batch.
     */
    @PreDestroy
    public void shutdownFlush() {
        log.info("Flushing {} buffered log events on shutdown", buffer.size());
        while (!buffer.isEmpty()) {
            flush();
        }
    }
}
