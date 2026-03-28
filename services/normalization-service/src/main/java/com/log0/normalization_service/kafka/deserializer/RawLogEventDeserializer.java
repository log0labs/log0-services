package com.log0.normalization_service.kafka.deserializer;

import java.io.IOException;
import java.util.Map;

import org.apache.kafka.common.errors.SerializationException;
import org.apache.kafka.common.serialization.Deserializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.log0.normalization_service.dto.RawLogEvent;

/**
 * Kafka {@link Deserializer} that converts raw JSON bytes from the {@code raw-logs} topic
 * into {@link RawLogEvent} instances.
 *
 * Uses a Jackson {@link JsonMapper} with auto-discovered modules (e.g. {@code jackson-datatype-jsr310})
 * so that Java time types in the payload are handled without extra configuration.
 * Returns {@code null} for empty payloads and wraps Jackson errors in
 * {@link SerializationException} so the Kafka consumer can route them to the DLQ.
 */
public class RawLogEventDeserializer implements Deserializer<RawLogEvent> {
    private final ObjectMapper objectMapper = JsonMapper.builder()
            .findAndAddModules()
            .build();

    @Override
    public void configure(Map<String, ?> configs, boolean isKey) {
        // no-op
    }

    /**
     * Deserializes a JSON byte array into a {@link RawLogEvent}.
     *
     * @param topic the source topic name (unused, present for interface compliance)
     * @param data  the raw JSON bytes; returns {@code null} if {@code null} or empty
     * @throws SerializationException if the bytes cannot be parsed as a valid {@link RawLogEvent}
     */
    @Override
    public RawLogEvent deserialize(String topic, byte[] data) {
        if (data == null || data.length == 0) {
            return null;
        }

        try {
            return objectMapper.readValue(data, RawLogEvent.class);
        } catch (IOException e) {
            throw new SerializationException("Failed to deserialize RawLogEvent", e);
        }
    }

    @Override
    public void close() {
        // no-op
    }
}
