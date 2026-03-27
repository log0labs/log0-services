package com.log0.notification_service.kafka.serializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.notification_service.dto.DlqEvent;
import org.apache.kafka.common.serialization.Serializer;

public class DlqEventSerializer implements Serializer<DlqEvent> {
    private final ObjectMapper objectMapper = new ObjectMapper()
            .registerModule(new JavaTimeModule());

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
