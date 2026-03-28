package com.log0.clustering_service.model;

import java.time.Instant;
import java.time.temporal.ChronoUnit;

import lombok.EqualsAndHashCode;
import lombok.Getter;

/**
 * Immutable map key that identifies a unique cluster bucket: the combination of a tenant,
 * a log fingerprint, and a time-aligned window bucket. Equality and hashing are derived
 * from all three fields, so the same fingerprint in different windows produces distinct keys.
 */
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

    /**
     * Builds a {@code ClusterKey} by floor-aligning {@code timestamp} to the nearest
     * {@code windowDurationMinutes} boundary, ensuring all events within the same window
     * map to the same bucket regardless of their exact arrival time.
     *
     * @param windowDurationMinutes width of each time bucket in minutes; must be a positive divisor of 60 for predictable alignment
     */
    public static ClusterKey of(String tenantId, String fingerprint, Instant timestamp, int windowDurationMinutes) {
        long bucketMinutes = timestamp.truncatedTo(ChronoUnit.MINUTES).getEpochSecond() / 60;
        long alignedMinutes = (bucketMinutes / windowDurationMinutes) * windowDurationMinutes;
        String windowBucket = tenantId + ":" + fingerprint + ":" + alignedMinutes;
        return new ClusterKey(tenantId, fingerprint, windowBucket);
    }
}
