package com.log0.notification_service.dto;

import java.time.Instant;

import lombok.Builder;
import lombok.Getter;

@Getter
@Builder
public class DlqEvent {
    private Object originalEvent;
    private String errorMessage;
    private String failedAt;
    private Instant failedAtTs;
}
