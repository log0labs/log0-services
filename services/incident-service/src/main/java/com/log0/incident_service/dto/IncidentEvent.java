package com.log0.incident_service.dto;

import java.time.Instant;
import java.util.List;

import com.fasterxml.jackson.databind.annotation.JsonDeserialize;
import com.fasterxml.jackson.databind.annotation.JsonSerialize;
import com.fasterxml.jackson.datatype.jsr310.deser.InstantDeserializer;
import com.fasterxml.jackson.datatype.jsr310.ser.InstantSerializer;

import lombok.Data;

@Data
public class IncidentEvent {
    private String tenantId;
    private String fingerprint;
    private String serviceName;
    private String environment;
    private String severity;
    private Long occurrenceCount;

    @JsonSerialize(using = InstantSerializer.class)
    @JsonDeserialize(using = InstantDeserializer.class)
    private Instant firstSeenAt;

    @JsonSerialize(using = InstantSerializer.class)
    @JsonDeserialize(using = InstantDeserializer.class)
    private Instant lastSeenAt;

    private List<String> topMessages;
}
