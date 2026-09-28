package com.log0.incident_service.async;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;

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

    private RestTemplate restTemplate;

    /**
     * {@link RestTemplate} with Boot's default Jackson converters (unlike {@code RestClient},
     * which was posting an empty body for {@link AiSummaryRequest}).
     */
    @PostConstruct
    private void init() {
        this.restTemplate = new RestTemplate();
    }

    /**
     * Submits an AI summary request to the AI Summary Service for the given incident.
     * Executes asynchronously on a separate thread so the incident creation transaction
     * can commit without waiting for the LLM response.
     *
     * @param incident the newly created incident to generate a summary for
     */
    @Async
    public void requestSummary(Incident incident) {
        try {
            log.info("Requesting AI summary for incident {}", incident.getIncidentId());

            AiSummaryRequest request = new AiSummaryRequest(
                    incident.getIncidentId(),
                    incident.getTenantId().toString(),
                    incident.getServiceName(),
                    incident.getEnvironment(),
                    incident.getSeverity(),
                    incident.getOccurrenceCount(),
                    incident.getFirstSeenAt(),
                    incident.getLastSeenAt(),
                    incident.getTopMessages());

            HttpHeaders headers = new HttpHeaders();
            headers.setContentType(MediaType.APPLICATION_JSON);
            HttpEntity<AiSummaryRequest> entity = new HttpEntity<>(request, headers);

            restTemplate.postForEntity(
                    aiServiceBaseUrl + "/api/v1/summaries",
                    entity,
                    Void.class);

            log.info("AI summary requested for incident {}", incident.getIncidentId());
        } catch (Exception e) {
            log.error("Failed to request AI summary for incident {}: {}",
                    incident.getIncidentId(), e.getMessage(), e);
        }
    }
}
