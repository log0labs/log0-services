package com.log0.normalisation_service.dto;

import lombok.Data;

import java.time.Instant;

@Data
public class RawLogEvent {
    private String eventId;
    private String tenantId;

    private String serviceName;
    private String environment;

    private Instant receivedAt;
    private Instant logTimestamp;

    private String level;
    private String message;
    private String trace;
}
