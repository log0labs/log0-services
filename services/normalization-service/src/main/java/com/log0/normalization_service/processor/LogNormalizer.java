package com.log0.normalization_service.processor;

import java.time.Instant;
import java.util.HashMap;
import java.util.Map;

import org.springframework.stereotype.Component;

import com.log0.normalization_service.dto.NormalizedLogEvent;
import com.log0.normalization_service.dto.RawLogEvent;

@Component
public class LogNormalizer {
    public NormalizedLogEvent normalize(RawLogEvent raw) {
        return NormalizedLogEvent.builder()
                .eventId(raw.getEventId())
                .tenantId(raw.getTenantId())
                .serviceName(raw.getServiceName())
                .environment(raw.getEnvironment())
                .timestamp(resolveTimestamp(raw))
                .level(normalizeLevel(raw.getLevel()))
                .message(normalizeMessage(raw.getMessage()))
                .attributes(extractAttributes(raw))
                .traceId(raw.getTrace())
                .schemaVersion("v1")
                .build();
    }

    private Instant resolveTimestamp(RawLogEvent raw) {
        return raw.getLogTimestamp() != null
                ? raw.getLogTimestamp()
                : raw.getReceivedAt() != null
                        ? raw.getReceivedAt()
                        : Instant.now();
    }

    private String normalizeLevel(String level) {
        if (level == null)
            return "INFO";
        return level.trim().toUpperCase();
    }

    private String normalizeMessage(String message) {
        return message == null ? "" : message.trim();
    }

    private Map<String, Object> extractAttributes(RawLogEvent raw) {
        // Placeholder for attribute extraction logic
        return new HashMap<>();
    }
}
