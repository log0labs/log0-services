package com.log0.clustering_service.dlq;

import java.time.Instant;

import lombok.Builder;
import lombok.Getter;

@Getter
@Builder
public class DlqEvent {
    private final Object originalEvent;
    private final String errorMessage;
    private final String failedAt;
    private final Instant failedAtTs;
}
