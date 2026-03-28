package com.log0.normalization_service.dlq;

import com.log0.normalization_service.kafka.KafkaTopics;
import lombok.RequiredArgsConstructor;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

/**
 * Publishes failed log events to the {@code raw-logs-dlq} dead-letter topic.
 *
 * Called whenever the normalization pipeline cannot process a {@code RawLogEvent},
 * ensuring no message is silently dropped. The original Kafka message key is
 * preserved so downstream DLQ consumers can correlate failures back to their source.
 */
@Component
@RequiredArgsConstructor
public class DlqProducer {
    private final KafkaTemplate<String, DlqEvent> kafkaTemplate;

    /**
     * Sends {@code event} to {@code raw-logs-dlq} under the given {@code key}.
     *
     * @param key   the Kafka message key from the original {@code raw-logs} record,
     *              used to preserve partition affinity and aid failure tracing
     * @param event the DLQ envelope containing the raw payload and failure reason
     */
    public void publish(String key, DlqEvent event) {
        kafkaTemplate.send(KafkaTopics.RAW_LOGS_DLQ, key, event);
    }
}
