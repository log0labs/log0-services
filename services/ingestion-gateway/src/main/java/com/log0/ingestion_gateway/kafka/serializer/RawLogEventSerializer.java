package com.log0.ingestion_gateway.kafka.serializer;

import java.util.Map;

import org.apache.kafka.common.errors.SerializationException;
import org.apache.kafka.common.serialization.Serializer;

import com.log0.ingestion_gateway.dto.RawLogEvent;

import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

public class RawLogEventSerializer implements Serializer<RawLogEvent> {

    private final ObjectMapper objectMapper = JsonMapper.builder()
            .findAndAddModules()
            .build();

    @Override
    public void configure(Map<String, ?> configs, boolean isKey) {
        // no-op
    }

    @Override
    public byte[] serialize(String topic, RawLogEvent data) {
        if (data == null) {
            return new byte[0];
        }

        try {
            return objectMapper.writeValueAsBytes(data);
        } catch (Exception e) {
            throw new SerializationException("Failed to serialize RawLogEvent", e);
        }
    }

    @Override
    public void close() {
        // no-op
    }
}
