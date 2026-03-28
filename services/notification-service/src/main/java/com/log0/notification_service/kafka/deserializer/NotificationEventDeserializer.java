package com.log0.notification_service.kafka.deserializer;

import java.io.IOException;

import org.apache.kafka.common.serialization.Deserializer;

import com.fasterxml.jackson.databind.json.JsonMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.notification_service.dto.NotificationEvent;

/**
 * Kafka {@link Deserializer} that converts raw JSON bytes from the {@code notification-events}
 * topic into {@link NotificationEvent} instances. Registers {@link JavaTimeModule} to handle
 * {@code java.time} fields; throws an unchecked exception on malformed payloads so the
 * consumer's error handler can route the record to the DLQ.
 */
public class NotificationEventDeserializer implements Deserializer<NotificationEvent> {
    private final JsonMapper objectMapper = JsonMapper.builder().addModule(new JavaTimeModule()).build();

    /**
     * Deserializes a JSON byte array into a {@link NotificationEvent}.
     *
     * @param topic the source topic (unused, required by the {@link Deserializer} contract)
     * @param data  the raw message bytes; returns {@code null} if {@code data} is {@code null}
     * @throws RuntimeException if the bytes cannot be parsed as a valid {@link NotificationEvent}
     */
    @Override
    public NotificationEvent deserialize(String topic, byte[] data) {
        if (data == null)
            return null;
        try {
            return objectMapper.readValue(data, NotificationEvent.class);
        } catch (IOException e) {
            throw new RuntimeException("Failed to deserialize NotificationEvent", e);
        }
    }
}
