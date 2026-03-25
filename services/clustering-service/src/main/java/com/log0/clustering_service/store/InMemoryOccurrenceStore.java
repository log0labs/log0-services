package com.log0.clustering_service.store;

import java.time.Instant;
import java.util.concurrent.ConcurrentHashMap;

import org.springframework.stereotype.Component;

import com.log0.clustering_service.config.ClusteringConfig;
import com.log0.clustering_service.model.ClusterKey;
import com.log0.clustering_service.model.OccurrenceWindow;

import lombok.RequiredArgsConstructor;

@Component
@RequiredArgsConstructor
public class InMemoryOccurrenceStore implements OccurrenceStore {
    private final ClusteringConfig config;
    private final ConcurrentHashMap<ClusterKey, OccurrenceWindow> store = new ConcurrentHashMap<>();

    @Override
    public OccurrenceWindow increment(ClusterKey key, String message) {
        OccurrenceWindow window = store.computeIfAbsent(key,
                k -> new OccurrenceWindow(Instant.now(), config.getMaxTopMessages()));
        window.record(message, Instant.now());
        return window;
    }
}
