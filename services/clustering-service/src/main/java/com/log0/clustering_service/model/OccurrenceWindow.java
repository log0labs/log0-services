package com.log0.clustering_service.model;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import lombok.Getter;

/**
 * Mutable accumulator for a single cluster window, tracking how many times a fingerprint
 * has fired, the first and last observed timestamps, and a bounded sample of distinct log
 * messages. Not thread-safe - callers must synchronise externally (e.g. via
 * {@link java.util.concurrent.ConcurrentHashMap#computeIfAbsent}).
 */
@Getter
public class OccurrenceWindow {
    private long count;
    private final Instant firstSeenAt;
    private Instant lastSeenAt;
    private final Set<String> topMessages;
    private final int maxTopMessages;
    private boolean incidentEmitted;

    public OccurrenceWindow(Instant firstSeenAt, int maxTopMessages) {
        this.count = 0;
        this.firstSeenAt = firstSeenAt;
        this.lastSeenAt = firstSeenAt;
        this.topMessages = new LinkedHashSet<>();
        this.maxTopMessages = maxTopMessages;
        this.incidentEmitted = false;
    }

    /**
     * Returns {@code true} exactly once - the first time this window is asked to emit after
     * crossing the occurrence threshold - and {@code false} on every subsequent call. This
     * lets the clusterer publish a single {@link com.log0.clustering_service.dto.IncidentEvent}
     * per window instead of one per message; downstream the authoritative occurrence count is
     * derived from ClickHouse, so the event is only a "this fingerprint became an incident"
     * signal rather than a running total.
     */
    public synchronized boolean markIncidentEmitted() {
        if (incidentEmitted) {
            return false;
        }
        incidentEmitted = true;
        return true;
    }

    /**
     * Records one occurrence: increments the count, updates {@code lastSeenAt}, and adds
     * {@code message} to the sample set if capacity has not been reached. Null messages are
     * silently ignored.
     */
    public void record(String message, Instant timestamp) {
        count++;
        lastSeenAt = timestamp;

        if (topMessages.size() < maxTopMessages && message != null) {
            topMessages.add(message);
        }
    }

    public List<String> getTopMessagesList() {
        return new ArrayList<>(topMessages);
    }
}
