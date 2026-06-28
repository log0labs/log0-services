package com.log0.normalization_service.consumer;

import java.time.Instant;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

import com.log0.normalization_service.clickhouse.LogEventRepository;
import com.log0.normalization_service.dlq.DlqEvent;
import com.log0.normalization_service.dlq.DlqProducer;
import com.log0.normalization_service.dto.NormalizedLogEvent;
import com.log0.normalization_service.dto.RawLogEvent;
import com.log0.normalization_service.kafka.producer.NormalizedLogProducer;
import com.log0.normalization_service.processor.LogNormalizer;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Kafka listener for the {@code raw-logs} topic (consumer group: {@code normalization-service}).
 *
 * <p>Each message is normalized and fingerprinted via {@link LogNormalizer}, then forwarded
 * to {@code normalized-logs}. On any processing failure the original event is routed to
 * {@code raw-logs-dlq} and the offset is always acknowledged to prevent partition blocking.
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class RawLogConsumer {

    private final LogNormalizer normalizer;
    private final NormalizedLogProducer producer;
    private final DlqProducer dlqProducer;
    private final LogEventRepository logEventRepository;

    /**
     * Fault-injection marker for chaos/DLQ testing. Empty by default (no effect). When set,
     * any raw log whose message contains this token is failed deliberately, exercising the
     * DLQ routing path without taking a real dependency down. Set via {@code fault.inject-marker}.
     */
    @Value("${fault.inject-marker:}")
    private String faultInjectMarker;

    /**
     * Processes a single {@link RawLogEvent} from {@code raw-logs}.
     *
     * <p>Normalizes and publishes the event, then acknowledges the offset. On failure,
     * wraps the original event in a {@link DlqEvent} keyed by {@code eventId} and
     * acknowledges regardless - ensuring the consumer never stalls on a bad message.
     *
     * @param event the deserialized raw log record
     * @param ack   manual acknowledgment handle; always called before returning
     */
    @KafkaListener(topics = "raw-logs", containerFactory = "kafkaListenerContainerFactory")
    public void consume(RawLogEvent event, Acknowledgment ack) {
        try {
            if (!faultInjectMarker.isBlank() && event.getMessage() != null
                    && event.getMessage().contains(faultInjectMarker)) {
                throw new IllegalStateException("fault-injection: poisoned message routed to DLQ");
            }
            NormalizedLogEvent normalized = normalizer.normalize(event);

            producer.publish(normalized);
            logEventRepository.save(normalized);
            ack.acknowledge();
        } catch (Exception e) {
            log.error("Error processing raw log message: {}", e.getMessage(), e);

            DlqEvent dlqEvent = DlqEvent.builder()
                    .originalEvent(event)
                    .errorMessage(e.getMessage())
                    .failedAt("normalization-service")
                    .failedAtTs(Instant.now())
                    .build();

            dlqProducer.publish(event.getEventId(), dlqEvent);

            ack.acknowledge();
        }
    }
}
