package com.log0.incident_service.dto;

import java.util.List;

import com.log0.incident_service.clickhouse.LogEventDto;

import lombok.Builder;
import lombok.Value;

/**
 * Paginated response for {@code GET /api/v1/logs}.
 */
@Value
@Builder
public class LogsPage {
    List<LogEventDto> content;
    long totalElements;
    int totalPages;
    int page;
    int size;
}
