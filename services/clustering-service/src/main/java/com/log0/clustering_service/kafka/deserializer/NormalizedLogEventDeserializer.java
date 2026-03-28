package com.log0.clustering_service.kafka.deserializer;

import java.io.IOException;

import org.apache.kafka.common.serialization.Deserializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.clustering_service.dto.NormalizedLogEvent;

/**
 * Kafka {@link Deserializer} that converts raw JSON bytes from the {@code normalized-logs} topic
 * into {@link NormalizedLogEvent} instances. Uses a {@link com.fasterxml.jackson.databind.ObjectMapper}
 * with {@link com.fasterxml.jackson.datatype.jsr310.JavaTimeModule} to handle {@code Instant} fields;
 * throws an unchecked exception on malformed payloads to trigger DLQ routing in the consumer.
 */
public class NormalizedLogEventDeserializer implements Deserializer<NormalizedLogEvent> {
    private final ObjectMapper objectMapper = new ObjectMapper().registerModule(new JavaTimeModule());

    @Override
    public NormalizedLogEvent deserialize(String topic, byte[] data) {
        try {
            return objectMapper.readValue(data, NormalizedLogEvent.class);
        } catch (IOException e) {
            throw new RuntimeException("Failed to deserialize NormalizedLogEvent", e);
        }
    }
}
