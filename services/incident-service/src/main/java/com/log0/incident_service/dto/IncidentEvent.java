package com.log0.incident_service.dto;

import java.time.Instant;
import java.util.List;

import lombok.Data;

@Data
public class IncidentEvent {
    private String tenantId;
    private String fingerprint;
    private String serviceName;
    private String environment;
    private String severity;
    private Long occurrenceCount;

    private Instant firstSeenAt;

    private Instant lastSeenAt;

    private List<String> topMessages;
}
