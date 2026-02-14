package com.log0.normalization_service.kafka.deserializer;

import java.util.Map;

import org.apache.kafka.common.errors.SerializationException;
import org.apache.kafka.common.serialization.Deserializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.log0.normalization_service.dto.RawLogEvent;

public class RawLogEventDeserializer implements Deserializer<RawLogEvent> {
    private final ObjectMapper objectMapper = JsonMapper.builder()
            .findAndAddModules()
            .build();

    @Override
    public void configure(Map<String, ?> configs, boolean isKey) {
        // no-op
    }

    @Override
    public RawLogEvent deserialize(String topic, byte[] data) {
        if (data == null || data.length == 0) {
            return null;
        }

        try {
            return objectMapper.readValue(data, RawLogEvent.class);
        } catch (Exception e) {
            throw new SerializationException("Failed to deserialize RawLogEvent", e);
        }
    }

    @Override
    public void close() {
        // no-op
    }
}
