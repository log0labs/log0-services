package com.log0.normalisation_service.kafka.producer;

import com.log0.normalisation_service.dto.NormalizedLogEvent;
import com.log0.normalisation_service.kafka.KafkaTopics;
import lombok.RequiredArgsConstructor;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

@Component
@RequiredArgsConstructor
public class NormalizedLogProducer {
    private final KafkaTemplate<String, NormalizedLogEvent> kafkaTemplate;

    public void publish(NormalizedLogEvent event) {
        kafkaTemplate.send(
                KafkaTopics.NORMALIZED_LOGS,
                event.getTenantId(),
                event
        );
    }
}
