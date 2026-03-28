package com.log0.clustering_service.store;

import com.log0.clustering_service.model.ClusterKey;
import com.log0.clustering_service.model.OccurrenceWindow;

/**
 * Storage abstraction for per-window occurrence counters. The default implementation is
 * in-memory; implementations must be safe to call concurrently from a multi-threaded
 * Kafka listener container.
 */
public interface OccurrenceStore {
    /**
     * Atomically creates or retrieves the {@link OccurrenceWindow} for {@code key}, records
     * one occurrence with {@code message}, and returns the updated window.
     *
     * @param key     the cluster bucket that identifies tenant + fingerprint + time window
     * @param message the raw log message to add to the top-messages sample; may be null
     * @return the {@link OccurrenceWindow} after the current event has been recorded
     */
    OccurrenceWindow increment(ClusterKey key, String message);
}
