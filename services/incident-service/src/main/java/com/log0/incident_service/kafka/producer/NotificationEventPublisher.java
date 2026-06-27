package com.log0.incident_service.kafka.producer;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.incident_service.dto.NotificationEvent;

import lombok.RequiredArgsConstructor;

/**
 * Kafka producer that publishes {@link NotificationEvent} messages to the
 * {@code notification-events} topic, keyed by {@code incidentId} so that all
 * notifications about the same incident (created, assigned, resolved) land on
 * the same partition and are processed in order.
 */
@Component
@RequiredArgsConstructor
public class NotificationEventPublisher {
    private static final String TOPIC = "notification-events";
    private final KafkaTemplate<String, NotificationEvent> notificationKafkaTemplate;

    /**
     * Publishes a {@link NotificationEvent} to {@code notification-events}, using the
     * event's {@code incidentId} as the Kafka message key to preserve per-incident ordering.
     */
    public void publish(NotificationEvent event) {
        notificationKafkaTemplate.send(TOPIC, event.getIncidentId(), event);
    }
}
