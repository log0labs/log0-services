package com.log0.incident_service.dlq;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import lombok.RequiredArgsConstructor;

/**
 * Kafka producer responsible for forwarding unprocessable events to the
 * {@code raw-logs-dlq} dead-letter topic. The incident fingerprint is used
 * as the message key to preserve per-fingerprint ordering in the DLQ.
 */
@Component
@RequiredArgsConstructor
public class DlqProducer {
    private static final String TOPIC = "raw-logs-dlq";
    private final KafkaTemplate<String, DlqEvent> dlqKafkaTemplate;

    /**
     * Publishes a {@link DlqEvent} to {@code raw-logs-dlq} keyed by the incident fingerprint.
     *
     * @param key   the incident fingerprint, used as the Kafka message key
     * @param event the dead-letter payload wrapping the original event and error context
     */
    public void publish(String key, DlqEvent event) {
        dlqKafkaTemplate.send(TOPIC, key, event);
    }
}
