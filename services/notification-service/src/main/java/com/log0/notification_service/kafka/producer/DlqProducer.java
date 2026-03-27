package com.log0.notification_service.kafka.producer;

import java.time.Instant;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.notification_service.dto.DlqEvent;

import lombok.RequiredArgsConstructor;

@Component
@RequiredArgsConstructor
public class DlqProducer {
    private static final String TOPIC = "notification-events-dlq";
    private final KafkaTemplate<String, DlqEvent> dlqKafkaTemplate;

    public void publish(String key, Object originalEvent, String errorMessage) {
        DlqEvent dlqEvent = DlqEvent.builder()
                .originalEvent(originalEvent)
                .errorMessage(errorMessage)
                .failedAt("notification-service")
                .failedAtTs(Instant.now())
                .build();
        dlqKafkaTemplate.send(TOPIC, key, dlqEvent);
    }
}
