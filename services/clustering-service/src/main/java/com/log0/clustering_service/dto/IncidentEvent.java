package com.log0.clustering_service.dto;

import java.time.Instant;
import java.util.List;

import lombok.Builder;
import lombok.Getter;

@Getter
@Builder
public class IncidentEvent {
    private final String tenantId;
    private final String fingerprint;
    private final String serviceName;
    private final String environment;
    private final String severity;
    private final Long occurrenceCount;
    private final Instant firstSeenAt;
    private final Instant lastSeenAt;
    private final List<String> topMessages;
}
