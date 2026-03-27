package com.log0.notification_service.kafka.deserializer;

import java.io.IOException;

import org.apache.kafka.common.serialization.Deserializer;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.notification_service.dto.NotificationEvent;

public class NotificationEventDeserializer implements Deserializer<NotificationEvent> {
    private final ObjectMapper objectMapper = new ObjectMapper()
            .registerModule(new JavaTimeModule());

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
