package com.log0.notification_service.kafka.producer;

import java.time.Instant;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.notification_service.dto.DlqEvent;

import lombok.RequiredArgsConstructor;

/**
 * Publishes unprocessable events to the {@code notification-events-dlq} dead-letter topic.
 * Wraps the original payload together with the error context so downstream consumers can
 * inspect or replay failures without data loss.
 */
@Component
@RequiredArgsConstructor
public class DlqProducer {
    private static final String TOPIC = "notification-events-dlq";
    private final KafkaTemplate<String, DlqEvent> dlqKafkaTemplate;

    /**
     * Builds a {@link DlqEvent} capturing the failure context and sends it to
     * {@code notification-events-dlq}, keyed so that all failures for the same tenant
     * land on the same partition.
     *
     * @param key           the Kafka message key, typically the {@code tenantId}
     * @param originalEvent the original event object that could not be processed
     * @param errorMessage  the error message from the caught exception
     */
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
