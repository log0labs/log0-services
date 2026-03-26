package com.log0.clustering_service.kafka.producer;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.clustering_service.dto.IncidentEvent;

import lombok.RequiredArgsConstructor;

@Component
@RequiredArgsConstructor
public class IncidentEventProducer {
    private static final String TOPIC = "incident-events";
    private final KafkaTemplate<String, IncidentEvent> kafkaTemplate;

    public void publish(IncidentEvent event) {
        kafkaTemplate.send(TOPIC, event.getTenantId(), event);
    }
}
