package com.log0.ingestion_gateway.kafka.producer;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.ingestion_gateway.dto.RawLogEvent;
import com.log0.ingestion_gateway.kafka.KafkaTopics;

@Component
public class RawLogProducer {
    private final KafkaTemplate<String, RawLogEvent> kafkaTemplate;

    public RawLogProducer(KafkaTemplate<String, RawLogEvent> kafkaTemplate) {
        this.kafkaTemplate = kafkaTemplate;
    }

    public void publish(RawLogEvent event) {
        kafkaTemplate.send(
                KafkaTopics.RAW_LOGS,
                event.getTenantId(),
                event);
    }
}
