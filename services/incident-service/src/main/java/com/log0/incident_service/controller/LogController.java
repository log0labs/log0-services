package com.log0.incident_service.controller;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.log0.incident_service.clickhouse.LogEventDto;
import com.log0.incident_service.clickhouse.LogEventRepository;
import com.log0.incident_service.dto.LogsPage;

import lombok.RequiredArgsConstructor;

/**
 * REST controller for the raw log explorer at {@code GET /api/v1/logs}.
 *
 * <p>Returns tenant-scoped log events from ClickHouse with optional filters
 * on service name, log level, and time range. All results are ordered
 * newest-first and returned as a paginated {@link LogsPage}.
 */
@RestController
@RequestMapping("/api/v1/logs")
@RequiredArgsConstructor
public class LogController {

    private final LogEventRepository logEventRepository;

    /**
     * List log events for the authenticated tenant with optional filters.
     *
     * @param tenantId    the tenant whose logs to return (required)
     * @param service     optional exact service name filter
     * @param level       optional log level filter (ERROR, WARN, INFO, DEBUG)
     * @param from        optional ISO-8601 lower-bound timestamp (inclusive)
     * @param to          optional ISO-8601 upper-bound timestamp (inclusive)
     * @param page        0-based page index (default 0)
     * @param size        rows per page, max 200 (default 50)
     */
    @GetMapping
    public ResponseEntity<LogsPage> listLogs(
            @RequestParam UUID tenantId,
            @RequestParam(required = false) String service,
            @RequestParam(required = false) String level,
            @RequestParam(required = false) Instant from,
            @RequestParam(required = false) Instant to,
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "50") int size) {

        int effectiveSize = Math.min(size, 200);
        int offset = page * effectiveSize;

        List<LogEventDto> content = logEventRepository.findByTenantIdWithFilters(
                tenantId, service, level, from, to, effectiveSize, offset);

        long total = logEventRepository.countByTenantIdWithFilters(
                tenantId, service, level, from, to);

        int totalPages = (int) Math.ceil((double) total / effectiveSize);

        LogsPage response = LogsPage.builder()
                .content(content)
                .totalElements(total)
                .totalPages(Math.max(1, totalPages))
                .page(page)
                .size(effectiveSize)
                .build();

        return ResponseEntity.ok(response);
    }
}
