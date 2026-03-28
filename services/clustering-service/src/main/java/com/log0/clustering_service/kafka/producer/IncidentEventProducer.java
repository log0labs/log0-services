package com.log0.clustering_service.kafka.producer;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.clustering_service.dto.IncidentEvent;

import lombok.RequiredArgsConstructor;

/**
 * Publishes {@link IncidentEvent} records to the {@code incident-events} Kafka topic.
 * Events are keyed by {@code tenantId} to ensure all incidents for the same tenant land
 * on the same partition and are consumed in order by downstream services.
 */
@Component
@RequiredArgsConstructor
public class IncidentEventProducer {
    private static final String TOPIC = "incident-events";
    private final KafkaTemplate<String, IncidentEvent> kafkaTemplate;

    public void publish(IncidentEvent event) {
        kafkaTemplate.send(TOPIC, event.getTenantId(), event);
    }
}
