package com.log0.ingestion_gateway.kafka.serializer;

import java.util.Map;

import org.apache.kafka.common.errors.SerializationException;
import org.apache.kafka.common.serialization.Serializer;

import com.log0.ingestion_gateway.dto.RawLogEvent;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * Kafka {@link Serializer} that converts a {@link RawLogEvent} to a JSON byte array
 * for publication to the {@code raw-logs} topic.
 * Uses a module-aware {@link ObjectMapper} so Java-time types (e.g. {@code Instant})
 * are serialized correctly without additional configuration.
 */
public class RawLogEventSerializer implements Serializer<RawLogEvent> {

    private final ObjectMapper objectMapper = JsonMapper.builder()
            .findAndAddModules()
            .build();

    @Override
    public void configure(Map<String, ?> configs, boolean isKey) {
        // no-op
    }

    /**
     * Serializes {@code data} to JSON bytes; returns an empty byte array when
     * {@code data} is {@code null} rather than {@code null} itself, matching the
     * behaviour expected by the {@code raw-logs} topic consumers.
     *
     * @throws SerializationException if Jackson cannot process the object graph
     */
    @Override
    public byte[] serialize(String topic, RawLogEvent data) {
        if (data == null) {
            return new byte[0];
        }

        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (JacksonException e) {
            throw new SerializationException("Failed to serialize RawLogEvent", e);
        }
    }

    @Override
    public void close() {
        // no-op
    }
}
