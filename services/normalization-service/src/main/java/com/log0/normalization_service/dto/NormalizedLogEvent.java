package com.log0.normalization_service.dto;

import lombok.Builder;
import lombok.Getter;

import java.time.Instant;
import java.util.Map;

@Getter
@Builder
public class NormalizedLogEvent {
    private final String eventId;
    private final String tenantId;

    private final String serviceName;
    private final String environment;

    private final Instant timestamp;

    private final String level;
    private final String message;

    private final Map<String, Object> attributes;

    private final String traceId;

    private final String schemaVersion;
}
