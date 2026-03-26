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

@Component
@RequiredArgsConstructor
public class FingerprintClusterer {
    private final OccurrenceStore occurrenceStore;
    private final IncidentEventProducer incidentEventProducer;
    private final ClusteringConfig config;
    private final SeverityResolver severityResolver;

    public void cluster(NormalizedLogEvent event) {
        ClusterKey key = ClusterKey.of(
                event.getTenantId(),
                event.getFingerprint(),
                event.getTimestamp(),
                config.getWindowDurationMinutes());

        OccurrenceWindow window = occurrenceStore.increment(key, event.getMessage());

        if (window.getCount() >= config.getOccurrenceThreshold()) {
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
