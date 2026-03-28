package com.log0.clustering_service.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import lombok.Getter;
import lombok.Setter;

/**
 * Externalized tuning knobs for the clustering pipeline, bound from the {@code clustering.*}
 * namespace in application properties. Governs how many occurrences within a time window
 * constitute an incident and how many distinct log messages are retained per window.
 */
@Component
@ConfigurationProperties(prefix = "clustering")
@Getter
@Setter
public class ClusteringConfig {
    private int occurrenceThreshold = 10;
    private int windowDurationMinutes = 5;
    private int maxTopMessages = 10;
}