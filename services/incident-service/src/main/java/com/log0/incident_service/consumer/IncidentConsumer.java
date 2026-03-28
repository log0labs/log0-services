package com.log0.incident_service.consumer;

import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

import com.log0.incident_service.dlq.DlqEvent;
import com.log0.incident_service.dlq.DlqProducer;
import com.log0.incident_service.dto.IncidentEvent;
import com.log0.incident_service.service.IncidentService;

import lombok.RequiredArgsConstructor;

/**
 * Kafka consumer that processes inbound {@code IncidentEvent} messages from the
 * {@code incident-events} topic. Uses manual acknowledgement so that a failed
 * record is forwarded to the DLQ ({@code raw-logs-dlq}) before the offset is
 * committed, preventing message loss on processing errors.
 */
@Component
@RequiredArgsConstructor
public class IncidentConsumer {
    private static final Logger log = LoggerFactory.getLogger(IncidentConsumer.class);
    private final IncidentService incidentService;
    private final DlqProducer dlqProducer;

    /**
     * Consumes a single {@code IncidentEvent} from the {@code incident-events} topic,
     * delegates processing to {@link IncidentService#createOrUpdateIncident}, and
     * acknowledges the offset. On failure, publishes the original event to the DLQ
     * before acknowledging so the partition is not blocked.
     *
     * @param record the raw Kafka record carrying the deserialized {@link IncidentEvent}
     * @param ack    manual acknowledgement handle; always called to advance the offset
     */
    @KafkaListener(topics = "incident-events", groupId = "incident-service")
    public void consume(ConsumerRecord<String, IncidentEvent> record, Acknowledgment ack) {
        IncidentEvent event = record.value();
        try {
            incidentService.createOrUpdateIncident(event);
            ack.acknowledge();
        } catch (Exception e) {
            log.error("Failed to process IncidentEvent tenantId={} fingerprint={} - sending to DLQ",
                    event.getTenantId(), event.getFingerprint(), e);
            dlqProducer.publish(event.getFingerprint(), DlqEvent.builder()
                    .originalEvent(event)
                    .errorMessage(e.getMessage())
                    .failedAt("incident-service")
                    .failedAtTs(java.time.Instant.now())
                    .build());
            ack.acknowledge();
        }
    }
}
