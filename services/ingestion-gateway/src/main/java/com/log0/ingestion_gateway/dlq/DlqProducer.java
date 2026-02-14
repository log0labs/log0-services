package com.log0.ingestion_gateway.dlq;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.ingestion_gateway.kafka.KafkaTopics;

import lombok.RequiredArgsConstructor;

@Component
@RequiredArgsConstructor
public class DlqProducer {
    private final KafkaTemplate<String, DlqEvent> kafkaTemplate;

    public void publish(String key, DlqEvent event) {
        kafkaTemplate.send(KafkaTopics.RAW_LOGS_DLQ, key, event);
    }
}
