package com.log0.clustering_service.model;

import java.time.Instant;
import java.time.temporal.ChronoUnit;

import lombok.EqualsAndHashCode;
import lombok.Getter;

@Getter
@EqualsAndHashCode
public class ClusterKey {
    private final String tenantId;
    private final String fingerprint;
    private final String windowBucket;

    private ClusterKey(String tenantId, String fingerprint, String windowBucket) {
        this.tenantId = tenantId;
        this.fingerprint = fingerprint;
        this.windowBucket = windowBucket;
    }

    public static ClusterKey of(String tenantId, String fingerprint, Instant timestamp, int windowDurationMinutes) {
        long bucketMinutes = timestamp.truncatedTo(ChronoUnit.MINUTES).getEpochSecond() / 60;
        long alignedMinutes = (bucketMinutes / windowDurationMinutes) * windowDurationMinutes;
        String windowBucket = tenantId + ":" + fingerprint + ":" + alignedMinutes;
        return new ClusterKey(tenantId, fingerprint, windowBucket);
    }
}
