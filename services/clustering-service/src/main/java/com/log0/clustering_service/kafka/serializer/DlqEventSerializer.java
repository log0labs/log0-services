package com.log0.clustering_service.kafka.serializer;

import java.io.IOException;

import org.apache.kafka.common.serialization.Serializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.clustering_service.dlq.DlqEvent;

/**
 * Kafka {@link Serializer} that converts a {@link DlqEvent} to JSON bytes for the
 * {@code raw-logs-dlq} topic. Includes {@link com.fasterxml.jackson.datatype.jsr310.JavaTimeModule}
 * so that {@code Instant} timestamps serialize correctly; throws an unchecked exception on
 * serialization failure to surface the error to the producer pipeline.
 */
public class DlqEventSerializer implements Serializer<DlqEvent> {

    private final ObjectMapper objectMapper = new ObjectMapper()
            .registerModule(new JavaTimeModule());

    @Override
    public byte[] serialize(String topic, DlqEvent data) {
        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (IOException e) {
            throw new RuntimeException("Failed to serialize DlqEvent", e);
        }
    }
}
