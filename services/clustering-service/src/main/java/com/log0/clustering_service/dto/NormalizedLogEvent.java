package com.log0.clustering_service.dto;

import java.time.Instant;
import java.util.Map;

import lombok.Data;

@Data
public class NormalizedLogEvent {
    private String eventId;
    private String tenantId;

    private String serviceName;
    private String environment;

    private Instant timestamp;

    private String level;
    private String message;
    private String messageTemplate;
    private String fingerprint;

    private Map<String, Object> attributes;

    private String traceId;

    private String schemaVersion;
}
