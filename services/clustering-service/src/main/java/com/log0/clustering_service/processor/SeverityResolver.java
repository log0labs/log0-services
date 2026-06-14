package com.log0.clustering_service.processor;

import org.springframework.stereotype.Component;

/**
 * Maps a raw log level string to a normalized incident severity used in {@link com.log0.clustering_service.dto.IncidentEvent}.
 * Unknown or null levels are treated as {@code HIGH} to err on the side of visibility.
 */
@Component
public class SeverityResolver {
    /**
     * Returns {@code "CRITICAL"} for FATAL, {@code "HIGH"} for ERROR (and null),
     * {@code "MEDIUM"} for WARN, and {@code "LOW"} for everything else.
     * Comparison is case-insensitive.
     *
     * @param level raw log level (e.g. {@code "ERROR"}, {@code "warn"}); may be null
     * @return normalized severity string: {@code "CRITICAL"}, {@code "HIGH"}, {@code "MEDIUM"}, or {@code "LOW"}
     */
    public String resolve(String level) {
        if (level == null)
            return "HIGH";

        return switch (level.toUpperCase()) {
            case "FATAL" -> "CRITICAL";
            case "ERROR" -> "HIGH";
            case "WARN" -> "MEDIUM";
            default -> "LOW";
        };
    }
}
