package com.log0.clustering_service.processor;

import org.springframework.stereotype.Component;

@Component
public class SeverityResolver {
    public String resolve(String level) {
        if (level == null)
            return "HIGH";

        return switch (level.toUpperCase()) {
            case "FATAL", "ERROR" -> "HIGH";
            case "WARN" -> "MEDIUM";
            default -> "LOW";
        };
    }
}
