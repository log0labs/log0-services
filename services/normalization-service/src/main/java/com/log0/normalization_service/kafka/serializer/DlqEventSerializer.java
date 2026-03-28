package com.log0.normalization_service.kafka.serializer;

import java.util.Map;

import org.apache.kafka.common.errors.SerializationException;
import org.apache.kafka.common.serialization.Serializer;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.log0.normalization_service.dlq.DlqEvent;

/**
 * Kafka {@link Serializer} that converts {@link DlqEvent} instances to JSON bytes
 * for the {@code raw-logs-dlq} topic.
 *
 * Throws {@link SerializationException} on failure so the Kafka producer propagates
 * the error rather than silently discarding a DLQ write.
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
     * Serializes a {@link DlqEvent} to a UTF-8 JSON byte array.
     *
     * @param topic the target topic name (unused, present for interface compliance)
     * @param data  the event to serialize; returns an empty byte array if {@code null}
     * @throws SerializationException if Jackson cannot serialize the event
     */
    @Override
    public byte[] serialize(String topic, DlqEvent data) {
        if (data == null) {
            return new byte[0];
        }

        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (JsonProcessingException e) {
            throw new SerializationException("Error serializing DlqEvent", e);
        }
    }

    @Override
    public void close() {
        // no-op
    }
}
