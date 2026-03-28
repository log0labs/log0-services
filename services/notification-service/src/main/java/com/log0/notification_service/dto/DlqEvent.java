package com.log0.notification_service.dto;

import java.time.Instant;

import lombok.Builder;
import lombok.Getter;

/**
 * Envelope published to the {@code notification-events-dlq} topic when Slack delivery fails.
 *
 * <p>{@code originalEvent} holds the {@code NotificationEvent} that could not be sent,
 * {@code failedAt} names the service that caught the error, and
 * {@code failedAtTs} records when the failure occurred for lag analysis.
 */
@Getter
@Builder
public class DlqEvent {
    private Object originalEvent;
    private String errorMessage;
    private String failedAt;
    private Instant failedAtTs;
}
