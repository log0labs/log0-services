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

@Component
@RequiredArgsConstructor
public class NormalizedLogConsumer {
    private static final Logger log = LoggerFactory.getLogger(NormalizedLogConsumer.class);

    private final FingerprintClusterer clusterer;
    private final DlqProducer dlqProducer;

    @KafkaListener(topics = "normalized-logs", groupId = "clustering-service")
    public void consume(ConsumerRecord<String, NormalizedLogEvent> record, Acknowledgment ack) {
        NormalizedLogEvent event = record.value();
        try {
            clusterer.cluster(event);
            ack.acknowledge();
        } catch (Exception e) {
            log.error("Failed to cluster event eventId={} tenantId={} fingerprint={} — sending to DLQ",
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
