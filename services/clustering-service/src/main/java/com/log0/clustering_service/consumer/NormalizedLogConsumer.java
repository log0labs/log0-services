package com.log0.clustering_service.consumer;

import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

import com.log0.clustering_service.dlq.DlqEvent;
import com.log0.clustering_service.dlq.DlqProducer;
import com.log0.clustering_service.dto.NormalizedLogEvent;
import com.log0.clustering_service.processor.FingerprintClusterer;

import lombok.RequiredArgsConstructor;

/**
 * Kafka consumer that drives the clustering pipeline by reading from the {@code normalized-logs}
 * topic (consumer group {@code clustering-service}). Each record is handed to
 * {@link com.log0.clustering_service.processor.FingerprintClusterer}; failures are forwarded
 * to the DLQ and the offset is still acknowledged to avoid poison-pill stalls.
 */
@Component
@RequiredArgsConstructor
public class NormalizedLogConsumer {
    private static final Logger log = LoggerFactory.getLogger(NormalizedLogConsumer.class);

    private final FingerprintClusterer clusterer;
    private final DlqProducer dlqProducer;

    /**
     * Triggered by each record on {@code normalized-logs}. Delegates clustering to
     * {@link com.log0.clustering_service.processor.FingerprintClusterer}; on any exception
     * publishes a {@link DlqEvent} to {@code raw-logs-dlq} before acknowledging, so the
     * consumer never blocks on a bad message.
     */
    @KafkaListener(topics = "normalized-logs", groupId = "clustering-service")
    public void consume(ConsumerRecord<String, NormalizedLogEvent> record, Acknowledgment ack) {
        NormalizedLogEvent event = record.value();
        try {
            clusterer.cluster(event);
            ack.acknowledge();
        } catch (Exception e) {
            log.error("Failed to cluster event eventId={} tenantId={} fingerprint={} - sending to DLQ",
                    event.getEventId(), event.getTenantId(), event.getFingerprint(), e);
            dlqProducer.publish(
                    event.getEventId(),
                    DlqEvent.builder()
                            .originalEvent(event)
                            .errorMessage(e.getMessage())
                            .failedAt("clustering-service")
                            .failedAtTs(java.time.Instant.now())
                            .build());
            ack.acknowledge();
        }
    }
}
