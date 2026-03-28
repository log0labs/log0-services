package com.log0.ingestion_gateway.kafka.serializer;

import java.util.Map;

import org.apache.kafka.common.errors.SerializationException;
import org.apache.kafka.common.serialization.Serializer;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.log0.ingestion_gateway.dlq.DlqEvent;

/**
 * Kafka {@link Serializer} that converts a {@link DlqEvent} to a JSON byte array
 * for publication to the {@code raw-logs-dlq} topic.
 * Uses a module-aware {@link ObjectMapper} so Java-time types (e.g. {@code Instant})
 * are serialized correctly without additional configuration.
 */
public class DlqEventSerializer implements Serializer<DlqEvent> {
    private final ObjectMapper objectMapper = JsonMapper.builder()
            .findAndAddModules()
            .build();

    @Override
    public void configure(Map<String, ?> configs, boolean isKey) {
        // no-op
    }

    /**
     * Serializes {@code data} to JSON bytes; returns {@code null} when {@code data}
     * is {@code null} to satisfy the Kafka producer's null-value contract.
     *
     * @throws SerializationException if Jackson cannot process the object graph
     */
    @Override
    public byte[] serialize(String topic, DlqEvent data) {
        if (data == null) {
            return null;
        }

        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (JsonProcessingException e) {
            throw new SerializationException("Failed to serialize DlqEvent", e);
        }
    }

    @Override
    public void close() {
        // no-op
    }
}
