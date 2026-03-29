package com.log0.incident_service.async;

import java.net.http.HttpClient;
import java.time.Duration;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import com.log0.incident_service.dto.AiSummaryRequest;
import com.log0.incident_service.entity.Incident;

import jakarta.annotation.PostConstruct;
import lombok.extern.slf4j.Slf4j;

/**
 * Fires asynchronous AI summary generation requests to the AI Summary Service.
 *
 * <p>
 * This class exists as a separate Spring bean from {@link com.log0.incident_service.service.IncidentService}
 * to avoid the Spring AOP self-invocation limitation: {@code @Async} only works when the
 * method is called through a Spring proxy. If the async method were inside
 * {@code IncidentService} and called via {@code this.method()}, the proxy would be bypassed
 * and the method would run synchronously on the same thread.
 *
 * <p>
 * The {@code @Async} annotation on {@link #requestSummary} causes Spring to submit the
 * method execution to the default task executor thread pool, returning immediately to the
 * caller. Any failure to reach the AI service is caught and logged without affecting the
 * incident creation flow.
 */
@Slf4j
@Component
public class AiSummarizer {

    @Value("${ai-service.base-url}")
    private String aiServiceBaseUrl;

    private RestClient restClient;

    /**
     * Initialises the {@link RestClient} with the AI service base URL after
     * the {@code @Value} field has been injected by Spring.
     */
    @PostConstruct
    private void init() {
        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();

        this.restClient = RestClient.builder()
                .baseUrl(aiServiceBaseUrl)
                .requestFactory(new JdkClientHttpRequestFactory(httpClient))
                .build();
    }

    /**
     * Submits an AI summary request to the AI Summary Service for the given incident.
     * Executes asynchronously on a separate thread so the incident creation transaction
     * can commit without waiting for the LLM response.
     *
     * <p>
     * All fields required by the AI service's {@code SummaryRequest} are mapped from
     * the {@link Incident} entity. Any exception during the HTTP call or JSON serialisation
     * is caught and logged — failure here must not propagate to the caller.
     *
     * @param incident the newly created incident to generate a summary for
     */
    @Async
    public void requestSummary(Incident incident) {
        try {
            log.info("Requesting AI summary for incident {}", incident.getIncidentId());

            AiSummaryRequest request = AiSummaryRequest.builder()
                    .incidentId(incident.getIncidentId())
                    .tenantId(incident.getTenantId().toString())
                    .serviceName(incident.getServiceName())
                    .environment(incident.getEnvironment())
                    .severity(incident.getSeverity())
                    .occurrenceCount(incident.getOccurrenceCount())
                    .firstSeenAt(incident.getFirstSeenAt())
                    .lastSeenAt(incident.getLastSeenAt())
                    .topMessages(incident.getTopMessages())
                    .build();

            restClient.post()
                    .uri("/api/v1/summaries")
                    .body(request)
                    .retrieve()
                    .toBodilessEntity();

            log.info("AI summary requested for incident {}", incident.getIncidentId());
        } catch (Exception e) {
            log.error("Failed to request AI summary for incident {}: {}",
                    incident.getIncidentId(), e.getMessage(), e);
        }
    }
}
