package com.log0.normalization_service.dlq;

import com.log0.normalization_service.kafka.KafkaTopics;
import lombok.RequiredArgsConstructor;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

@Component
@RequiredArgsConstructor
public class DlqProducer {
    private final KafkaTemplate<String, DlqEvent> kafkaTemplate;

    public void publish(String key, DlqEvent event) {
        kafkaTemplate.send(KafkaTopics.RAW_LOGS_DLQ, key, event);
    }
}
