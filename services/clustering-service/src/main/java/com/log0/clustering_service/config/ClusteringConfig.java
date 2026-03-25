package com.log0.clustering_service.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import lombok.Getter;
import lombok.Setter;

@Component
@ConfigurationProperties(prefix = "clustering")
@Getter
@Setter
public class ClusteringConfig {
    private int occurrenceThreshold = 10;
    private int windowDurationMinutes = 5;
    private int maxTopMessages = 10;
}