package com.log0.normalization_service.dlq;

import lombok.Builder;
import lombok.Getter;

import java.time.Instant;

@Getter
@Builder
public class DlqEvent {
    private final Object originalEvent;
    private final String errorMessage;
    private final String failedAt;
    private final Instant failedAtTs;
}
