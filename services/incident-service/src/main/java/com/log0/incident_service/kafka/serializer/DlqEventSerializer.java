package com.log0.incident_service.kafka.serializer;

import org.apache.kafka.common.serialization.Serializer;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.incident_service.dlq.DlqEvent;

/**
 * Kafka {@link Serializer} that encodes {@link DlqEvent} objects to JSON bytes for
 * publication to the {@code raw-logs-dlq} topic. Registered in {@link
 * com.log0.incident_service.kafka.config.KafkaProducerConfig} on the DLQ producer factory.
 */
public class DlqEventSerializer implements Serializer<DlqEvent> {

    private final ObjectMapper objectMapper = new ObjectMapper()
            .registerModule(new JavaTimeModule());

    @Override
    public byte[] serialize(String topic, DlqEvent data) {
        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("Failed to serialize DlqEvent", e);
        }
    }
}
