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

@Component
@RequiredArgsConstructor
public class RawLogProducer {
    private static final Logger log = LoggerFactory.getLogger(RawLogProducer.class);
    private final KafkaTemplate<String, RawLogEvent> kafkaTemplate;
    private final DlqProducer dlqProducer;

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
