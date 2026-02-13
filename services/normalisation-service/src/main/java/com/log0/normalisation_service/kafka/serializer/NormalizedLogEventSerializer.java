package com.log0.normalisation_service.kafka.serializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.log0.normalisation_service.dto.NormalizedLogEvent;
import org.apache.kafka.common.serialization.Serializer;

import java.util.Map;

public class NormalizedLogEventSerializer implements Serializer<NormalizedLogEvent> {
    private final ObjectMapper objectMapper = JsonMapper.builder()
            .findAndAddModules()
            .build();

    @Override
    public void configure(Map<String, ?> configs, boolean isKey) {
        // no-op
    }

    @Override
    public byte[] serialize(String topic, NormalizedLogEvent data) {
        if (data == null) {
            return new byte[0];
        }

        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize NormalizedLogEvent", e);
        }
    }

    @Override
    public void close() {
        // no-op
    }
}
