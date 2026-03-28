package com.log0.ingestion_gateway.kafka.producer;

import java.time.Instant;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.log0.ingestion_gateway.dlq.DlqEvent;
import com.log0.ingestion_gateway.dlq.DlqProducer;
import com.log0.ingestion_gateway.dto.RawLogEvent;
import com.log0.ingestion_gateway.kafka.KafkaTopics;

import lombok.RequiredArgsConstructor;

/**
 * Publishes {@link RawLogEvent} records to the {@code raw-logs} Kafka topic, keyed
 * by {@code tenantId} to co-locate a tenant's events in the same partition.
 * On delivery failure the event is automatically routed to {@code raw-logs-dlq}
 * via {@link DlqProducer} so no log data is silently discarded.
 */
@Component
@RequiredArgsConstructor
public class RawLogProducer {
    private static final Logger log = LoggerFactory.getLogger(RawLogProducer.class);
    private final KafkaTemplate<String, RawLogEvent> kafkaTemplate;
    private final DlqProducer dlqProducer;

    /**
     * Sends {@code event} to {@code raw-logs} and registers an async completion
     * callback; if the send fails the event is wrapped in a {@link DlqEvent} and
     * forwarded to {@code raw-logs-dlq} keyed by the original {@code eventId}.
     *
     * @param event the log event to publish; must have a non-null {@code tenantId}
     *              and {@code eventId}
     */
    public void publish(RawLogEvent event) {
        kafkaTemplate.send(
                KafkaTopics.RAW_LOGS,
                event.getTenantId(),
                event).whenComplete((result, ex) -> {
                    if (ex != null) {
                        log.error("Failed to publish raw log event: {}", ex.getMessage(), ex);

                        DlqEvent dlqEvent = DlqEvent.builder()
                                .originalEvent(event)
                                .errorMessage(ex.getMessage())
                                .failedAt("ingestion-gateway")
                                .failedAtTs(Instant.now())
                                .build();

                        dlqProducer.publish(event.getEventId(), dlqEvent);
                    }
                });
    }
}
