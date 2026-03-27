package com.log0.incident_service.kafka.producer;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.incident_service.dto.NotificationEvent;

import lombok.RequiredArgsConstructor;

@Component
@RequiredArgsConstructor
public class NotificationEventPublisher {
    private static final String TOPIC = "notification-events";
    private final KafkaTemplate<String, NotificationEvent> notificationKafkaTemplate;

    public void publish(NotificationEvent event) {
        notificationKafkaTemplate.send(TOPIC, event.getTenantId(), event);
    }
}
