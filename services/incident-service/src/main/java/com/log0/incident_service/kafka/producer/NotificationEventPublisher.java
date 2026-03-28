package com.log0.incident_service.kafka.producer;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.incident_service.dto.NotificationEvent;

import lombok.RequiredArgsConstructor;

/**
 * Kafka producer that publishes {@link NotificationEvent} messages to the
 * {@code notification-events} topic, keyed by {@code tenantId} to ensure
 * per-tenant ordering within a partition.
 */
@Component
@RequiredArgsConstructor
public class NotificationEventPublisher {
    private static final String TOPIC = "notification-events";
    private final KafkaTemplate<String, NotificationEvent> notificationKafkaTemplate;

    /**
     * Publishes a {@link NotificationEvent} to {@code notification-events}, using the
     * event's {@code tenantId} as the Kafka message key.
     */
    public void publish(NotificationEvent event) {
        notificationKafkaTemplate.send(TOPIC, event.getTenantId(), event);
    }
}
