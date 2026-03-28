package com.log0.ingestion_gateway.dlq;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.ingestion_gateway.kafka.KafkaTopics;

import lombok.RequiredArgsConstructor;

/**
 * Publishes failed log events to the {@code raw-logs-dlq} dead-letter topic so they
 * can be inspected or replayed without being silently dropped.
 * This producer is fire-and-forget; callers are responsible for logging before
 * invoking {@link #publish}.
 */
@Component
@RequiredArgsConstructor
public class DlqProducer {
    private final KafkaTemplate<String, DlqEvent> kafkaTemplate;

    /**
     * Sends a {@link DlqEvent} to {@code raw-logs-dlq}, keyed by the original event ID
     * to preserve per-event ordering within the DLQ partition.
     *
     * @param key   partition key - typically the original event's {@code eventId}
     * @param event the dead-letter payload wrapping the failed event and error details
     */
    public void publish(String key, DlqEvent event) {
        kafkaTemplate.send(KafkaTopics.RAW_LOGS_DLQ, key, event);
    }
}
