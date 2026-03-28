package com.log0.normalization_service.kafka.serializer;

import java.util.Map;

import org.apache.kafka.common.serialization.Serializer;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.log0.normalization_service.dto.NormalizedLogEvent;

/**
 * Kafka {@link Serializer} that converts {@link NormalizedLogEvent} instances to JSON bytes
 * for the {@code normalized-logs} topic.
 *
 * Uses a Jackson {@link com.fasterxml.jackson.databind.json.JsonMapper} with auto-discovered
 * modules so that Java time types ({@code Instant}, etc.) are serialized correctly without
 * additional configuration.
 */
public class NormalizedLogEventSerializer implements Serializer<NormalizedLogEvent> {
    private final ObjectMapper objectMapper = JsonMapper.builder()
            .findAndAddModules()
            .build();

    @Override
    public void configure(Map<String, ?> configs, boolean isKey) {
        // no-op
    }

    /**
     * Serializes a {@link NormalizedLogEvent} to a UTF-8 JSON byte array.
     *
     * @param topic the target topic name (unused, present for interface compliance)
     * @param data  the event to serialize; returns an empty byte array if {@code null}
     * @throws RuntimeException if Jackson cannot serialize the event
     */
    @Override
    public byte[] serialize(String topic, NormalizedLogEvent data) {
        if (data == null) {
            return new byte[0];
        }

        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("Failed to serialize NormalizedLogEvent", e);
        }
    }

    @Override
    public void close() {
        // no-op
    }
}
