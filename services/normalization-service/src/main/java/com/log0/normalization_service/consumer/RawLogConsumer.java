package com.log0.normalization_service.consumer;

import java.time.Instant;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

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
            NormalizedLogEvent normalized = normalizer.normalize(event);

            producer.publish(normalized);

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
