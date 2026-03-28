package com.log0.notification_service.kafka.serializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.notification_service.dto.DlqEvent;
import org.apache.kafka.common.serialization.Serializer;

/**
 * Kafka {@link Serializer} that converts {@link DlqEvent} objects to JSON bytes for
 * publication to the {@code notification-events-dlq} topic. Registers {@link JavaTimeModule}
 * to correctly serialise {@code java.time} fields such as {@code failedAtTs}.
 */
public class DlqEventSerializer implements Serializer<DlqEvent> {
    private final ObjectMapper objectMapper = new ObjectMapper()
            .registerModule(new JavaTimeModule());

    /**
     * Serializes a {@link DlqEvent} to a JSON byte array.
     *
     * @param topic the destination topic (unused, required by the {@link Serializer} contract)
     * @param data  the event to serialize; returns {@code null} if {@code data} is {@code null}
     * @throws RuntimeException if Jackson cannot marshal the event
     */
    @Override
    public byte[] serialize(String topic, DlqEvent data) {
        if (data == null) return null;
        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize DlqEvent", e);
        }
    }
}
