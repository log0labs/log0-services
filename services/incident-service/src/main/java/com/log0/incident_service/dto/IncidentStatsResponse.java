package com.log0.incident_service.dto;

import java.util.List;
import java.util.Map;

/**
 * Counts for the dashboard, computed over all of a tenant's incidents in the database rather
 * than over whatever page the console happened to load.
 *
 * @param total        how many incidents the tenant has
 * @param byStatus     count per status ({@code NEW}, {@code ASSIGNED}, ...); absent statuses are 0
 * @param bySeverity   count per severity ({@code CRITICAL}, ...); absent severities are 0
 * @param environments the distinct environments, for the filter dropdown
 */
public record IncidentStatsResponse(
        long total,
        Map<String, Long> byStatus,
        Map<String, Long> bySeverity,
        List<String> environments) {
}
