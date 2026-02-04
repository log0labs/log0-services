package com.log0.ingestion_gateway.dto;

import java.time.Instant;

import lombok.Builder;
import lombok.Getter;

@Getter
@Builder
public class RawLogEvent {

    private final String eventId;
    private final String tenantId;

    private final String serviceName;
    private final String environment;

    private final Instant receivedAt;
    private final Instant logTimestamp;

    private final String level;
    private final String message;
    private final String trace;
}
