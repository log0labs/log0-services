package com.log0.clustering_service.kafka.deserializer;

import java.io.IOException;

import org.apache.kafka.common.serialization.Deserializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.clustering_service.dto.NormalizedLogEvent;

public class NormalizedLogEventDeserializer implements Deserializer<NormalizedLogEvent> {
    private final ObjectMapper objectMapper = new ObjectMapper().registerModule(new JavaTimeModule());

    @Override
    public NormalizedLogEvent deserialize(String topic, byte[] data) {
        try {
            return objectMapper.readValue(data, NormalizedLogEvent.class);
        } catch (IOException e) {
            throw new RuntimeException("Failed to deserialize NormalizedLogEvent", e);
        }
    }
}
