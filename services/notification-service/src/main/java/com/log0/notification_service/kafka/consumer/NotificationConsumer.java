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

/**
 * Kafka consumer that drives the notification delivery pipeline.
 * Listens on the {@code notification-events} topic (consumer group {@code notification-service}),
 * delegates each event to {@link SlackNotifier}, and routes any processing failure to
 * {@link DlqProducer} before acknowledging the offset - ensuring no message is ever silently lost.
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class NotificationConsumer {
    private final SlackNotifier slackNotifier;
    private final DlqProducer dlqProducer;

    /**
     * Triggered by each record on {@code notification-events}; attempts Slack delivery and,
     * on failure, publishes a {@link com.log0.notification_service.dto.DlqEvent} to
     * {@code notification-events-dlq} keyed by {@code tenantId}. The offset is always
     * acknowledged in the {@code finally} block regardless of outcome.
     *
     * @param record the raw Kafka record carrying the deserialized {@link NotificationEvent}
     * @param ack    manual acknowledgment handle; must be called to advance the consumer offset
     */
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
