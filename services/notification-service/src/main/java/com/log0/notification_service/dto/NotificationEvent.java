package com.log0.notification_service.dto;

import java.time.Instant;
import java.util.List;

import lombok.Data;

@Data
public class NotificationEvent {
    private String tenantId;
    private String incidentId;
    private String fingerprint;
    private String serviceName;
    private String environment;
    private String severity;
    private String status;
    private Long occurrenceCount;

    private Instant firstSeenAt;

    private Instant lastSeenAt;

    private List<String> topMessages;
    private String aiSummary;
    private String assignedToUserId;
    private String notificationType;
}
