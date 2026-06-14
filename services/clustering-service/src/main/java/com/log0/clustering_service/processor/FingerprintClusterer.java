package com.log0.clustering_service.processor;

import org.springframework.stereotype.Component;

import com.log0.clustering_service.config.ClusteringConfig;
import com.log0.clustering_service.dto.IncidentEvent;
import com.log0.clustering_service.dto.NormalizedLogEvent;
import com.log0.clustering_service.kafka.producer.IncidentEventProducer;
import com.log0.clustering_service.model.ClusterKey;
import com.log0.clustering_service.model.OccurrenceWindow;
import com.log0.clustering_service.store.OccurrenceStore;

import lombok.RequiredArgsConstructor;

/**
 * Core clustering logic: groups incoming {@link NormalizedLogEvent}s by fingerprint and
 * time window, and emits a single {@link IncidentEvent} to the {@code incident-events} topic
 * the first time a window's occurrence count reaches the configured threshold. Subsequent
 * messages in the same window do NOT re-emit - the window's {@code markIncidentEmitted} guard
 * ensures one event per (fingerprint, window). The authoritative occurrence count lives in
 * ClickHouse and is computed by the incident service on read, so the event carries only a
 * threshold-crossing signal, not a running total.
 */
@Component
@RequiredArgsConstructor
public class FingerprintClusterer {
    private final OccurrenceStore occurrenceStore;
    private final IncidentEventProducer incidentEventProducer;
    private final ClusteringConfig config;
    private final SeverityResolver severityResolver;

    /**
     * Accumulates {@code event} into its window and publishes an {@link IncidentEvent} if the
     * window's count has reached the occurrence threshold. Callers (the Kafka consumer) are
     * responsible for DLQ routing on any exception thrown here.
     */
    public void cluster(NormalizedLogEvent event) {
        ClusterKey key = ClusterKey.of(
                event.getTenantId(),
                event.getFingerprint(),
                event.getTimestamp(),
                config.getWindowDurationMinutes());

        OccurrenceWindow window = occurrenceStore.increment(key, event.getMessage());

        if (window.getCount() >= config.getOccurrenceThreshold() && window.markIncidentEmitted()) {
            IncidentEvent incidentEvent = buildIncidentEvent(event, window);
            incidentEventProducer.publish(incidentEvent);
        }
    }

    private IncidentEvent buildIncidentEvent(NormalizedLogEvent logEvent, OccurrenceWindow window) {
        return IncidentEvent.builder()
                .tenantId(logEvent.getTenantId())
                .fingerprint(logEvent.getFingerprint())
                .serviceName(logEvent.getServiceName())
                .environment(logEvent.getEnvironment())
                .severity(severityResolver.resolve(logEvent.getLevel()))
                .occurrenceCount(window.getCount())
                .firstSeenAt(window.getFirstSeenAt())
                .lastSeenAt(window.getLastSeenAt())
                .topMessages(window.getTopMessagesList())
                .build();
    }
}
