package com.log0.normalisation_service.consumer;

import com.log0.normalisation_service.dto.NormalizedLogEvent;
import com.log0.normalisation_service.kafka.producer.NormalizedLogProducer;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import com.log0.normalisation_service.dto.RawLogEvent;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

import java.util.Map;

@Slf4j
@Component
@RequiredArgsConstructor
public class RawLogConsumer {

    private final NormalizedLogProducer producer;

    @KafkaListener(
            topics = "raw-logs",
            containerFactory = "kafkaListenerContainerFactory"
    )
    public void consume(RawLogEvent event, Acknowledgment ack) {
        try {
            NormalizedLogEvent normalized = NormalizedLogEvent.builder()
                    .eventId(event.getEventId())
                    .tenantId(event.getTenantId())
                    .serviceName(event.getServiceName())
                    .environment(event.getEnvironment())
                    .timestamp(event.getLogTimestamp())
                    .level(event.getLevel())
                    .message(event.getMessage())
                    .attributes(Map.of()) // Placeholder for future attribute extraction logic
                    .traceId(event.getTrace())
                    .schemaVersion("v1")
                    .build();

            producer.publish(normalized);
        } catch (Exception e) {
            log.error("Error processing raw log message: {}", e.getMessage(), e);
        }
    }
}
