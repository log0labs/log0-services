package com.log0.normalization_service.kafka.producer;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.normalization_service.dto.NormalizedLogEvent;
import com.log0.normalization_service.kafka.KafkaTopics;

import lombok.RequiredArgsConstructor;

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
