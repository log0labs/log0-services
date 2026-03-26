package com.log0.clustering_service.dlq;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import lombok.RequiredArgsConstructor;

@Component
@RequiredArgsConstructor
public class DlqProducer {

    private static final String TOPIC = "raw-logs-dlq";
    private final KafkaTemplate<String, DlqEvent> kafkaTemplate;

    public void publish(String key, DlqEvent event) {
        kafkaTemplate.send(TOPIC, key, event);
    }
}
