package com.log0.ai_service.client;

import java.util.Map;
import java.util.UUID;

import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import com.log0.ai_service.config.AiConfig;

import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * REST client for writing AI-generated summaries back to the Incident Service.
 *
 * <p>
 * Calls {@code PATCH /api/v1/incidents/{incidentId}/ai-summary} on the
 * Incident Service once the LLM has returned a result. The base URL is
 * configured via {@code ai.incident-service-base-url} in
 * {@code application.yml}.
 *
 * <p>
 * This is the only outbound HTTP call the AI service makes that is not to
 * an LLM provider - it completes the async callback loop started by the
 * Incident Service.
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class IncidentServiceClient {
    private final AiConfig aiConfig;

    private RestClient restClient;

    /**
     * Initialises the {@link RestClient} with the Incident Service base URL
     * from {@code ai.incident-service-base-url} in {@code application.yml}.
     */
    @PostConstruct
    private void init() {
        this.restClient = RestClient.builder()
                .baseUrl(aiConfig.getIncidentServiceBaseUrl())
                .build();
    }

    /**
     * Sends the generated {@code summary} to the Incident Service via
     * {@code PATCH /api/v1/incidents/{incidentId}/ai-summary}.
     *
     * <p>
     * Uses an inline {@code Map} for the request body - the payload is a
     * single field used in one place, so a dedicated DTO class is not
     * warranted.
     *
     * @param incidentId the incident to update
     * @param summary    the AI-generated summary text to store
     * @throws org.springframework.web.client.RestClientException if the
     *                                                            Incident Service
     *                                                            is unreachable or
     *                                                            returns a non-2xx
     *                                                            status
     */
    public void updateAiSummary(UUID incidentId, String summary) {
        log.debug("Sending AI summary to incident-service for incident {}", incidentId);

        restClient.patch()
                .uri("/api/v1/incidents/{incidentId}/ai-summary", incidentId)
                .body(Map.of("aiSummary", summary))
                .retrieve()
                .toBodilessEntity();

        log.debug("AI summary delivered for incident {}", incidentId);
    }
}
