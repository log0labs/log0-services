package com.log0.normalization_service.kafka.serializer;

import java.util.Map;

import org.apache.kafka.common.errors.SerializationException;
import org.apache.kafka.common.serialization.Serializer;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.log0.normalization_service.dlq.DlqEvent;

public class DlqEventSerializer implements Serializer<DlqEvent> {
    private final ObjectMapper objectMapper = JsonMapper.builder()
            .findAndAddModules()
            .build();

    @Override
    public void configure(Map<String, ?> configs, boolean isKey) {
        // no-op
    }

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
