package com.log0.incident_service.kafka.deserializer;

import java.io.IOException;

import org.apache.kafka.common.serialization.Deserializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.incident_service.dto.IncidentEvent;

public class IncidentEventDeserializer implements Deserializer<IncidentEvent> {
    private final ObjectMapper objectMapper = new ObjectMapper().registerModule(new JavaTimeModule());

    @Override
    public IncidentEvent deserialize(String topic, byte[] data) {
        try {
            return objectMapper.readValue(data, IncidentEvent.class);
        } catch (IOException e) {
            throw new RuntimeException("Failed to deserialize IncidentEvent", e);
        }
    }
}
