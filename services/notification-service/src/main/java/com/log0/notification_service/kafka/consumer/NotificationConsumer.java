package com.log0.notification_service.kafka.consumer;

import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

import com.log0.notification_service.dto.NotificationEvent;
import com.log0.notification_service.kafka.producer.DlqProducer;
import com.log0.notification_service.service.SlackNotifier;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

@Slf4j
@Component
@RequiredArgsConstructor
public class NotificationConsumer {
    private final SlackNotifier slackNotifier;
    private final DlqProducer dlqProducer;

    @KafkaListener(topics = "notification-events", groupId = "notification-service")
    public void consume(ConsumerRecord<String, NotificationEvent> record, Acknowledgment ack) {
        NotificationEvent event = record.value();
        try {
            slackNotifier.notify(event);
        } catch (Exception e) {
            log.error("Failed to process notification event for incident {}: {}",
                    event.getIncidentId(), e.getMessage());
            dlqProducer.publish(event.getTenantId(), event, e.getMessage());
        } finally {
            ack.acknowledge();
        }
    }
}
