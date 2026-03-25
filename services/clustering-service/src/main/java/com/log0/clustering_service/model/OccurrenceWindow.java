package com.log0.clustering_service.model;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import lombok.Getter;

@Getter
public class OccurrenceWindow {
    private long count;
    private final Instant firstSeenAt;
    private Instant lastSeenAt;
    private final Set<String> topMessages;
    private final int maxTopMessages;

    public OccurrenceWindow(Instant firstSeenAt, int maxTopMessages) {
        this.count = 0;
        this.firstSeenAt = firstSeenAt;
        this.lastSeenAt = firstSeenAt;
        this.topMessages = new LinkedHashSet<>();
        this.maxTopMessages = maxTopMessages;
    }

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
