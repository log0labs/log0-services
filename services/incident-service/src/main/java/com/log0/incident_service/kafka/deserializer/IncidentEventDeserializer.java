package com.log0.incident_service.kafka.deserializer;

import java.io.IOException;

import org.apache.kafka.common.serialization.Deserializer;

import com.fasterxml.jackson.databind.json.JsonMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.incident_service.dto.IncidentEvent;

/**
 * Kafka {@link Deserializer} that converts raw JSON bytes from the {@code incident-events}
 * topic into {@link IncidentEvent} instances. Uses {@link JavaTimeModule} to handle
 * {@code java.time} types; throws an unchecked exception on malformed payloads so the
 * consumer can route the record to the DLQ rather than silently skipping it.
 */
public class IncidentEventDeserializer implements Deserializer<IncidentEvent> {
    private final JsonMapper objectMapper = JsonMapper.builder().addModule(new JavaTimeModule()).build();

    @Override
    public IncidentEvent deserialize(String topic, byte[] data) {
        try {
            return objectMapper.readValue(data, IncidentEvent.class);
        } catch (IOException e) {
            throw new RuntimeException("Failed to deserialize IncidentEvent", e);
        }
    }
}
