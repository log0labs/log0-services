package com.log0.clustering_service.dlq;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import lombok.RequiredArgsConstructor;

/**
 * Publishes failed-processing records to the {@code raw-logs-dlq} Kafka topic so they can be
 * inspected or replayed without blocking the main pipeline. The event key is the original
 * {@code eventId}, preserving per-tenant ordering in the DLQ.
 */
@Component
@RequiredArgsConstructor
public class DlqProducer {

    private static final String TOPIC = "raw-logs-dlq";
    private final KafkaTemplate<String, DlqEvent> kafkaTemplate;

    public void publish(String key, DlqEvent event) {
        kafkaTemplate.send(TOPIC, key, event);
    }
}
