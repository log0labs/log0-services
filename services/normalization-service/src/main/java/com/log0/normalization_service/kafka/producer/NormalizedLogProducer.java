package com.log0.normalization_service.kafka.producer;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.normalization_service.dto.NormalizedLogEvent;
import com.log0.normalization_service.kafka.KafkaTopics;

import lombok.RequiredArgsConstructor;

/**
 * Publishes {@link NormalizedLogEvent} records to the {@code normalized-logs} topic.
 *
 * Uses {@code tenantId} as the Kafka message key so that all events for the same tenant
 * are routed to the same partition, preserving per-tenant ordering for downstream consumers.
 */
@Component
@RequiredArgsConstructor
public class NormalizedLogProducer {
    private final KafkaTemplate<String, NormalizedLogEvent> kafkaTemplate;

    public void publish(NormalizedLogEvent event) {
        kafkaTemplate.send(
                KafkaTopics.NORMALIZED_LOGS,
                event.getTenantId(),
                event);
    }
}
