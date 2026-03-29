package com.log0.ai_service.service;

import org.springframework.stereotype.Service;

import com.log0.ai_service.client.IncidentServiceClient;
import com.log0.ai_service.dto.SummaryRequest;
import com.log0.ai_service.prompt.PromptBuilder;
import com.log0.ai_service.provider.LlmProvider;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Orchestrates AI summary generation for a single incident.
 *
 * <p>
 * Coordinates three steps in sequence: build the prompt from incident
 * context via {@link PromptBuilder}, send it to the active {@link LlmProvider},
 * then write the result back to the Incident Service via
 * {@link IncidentServiceClient}. Any failure at any step is caught and logged
 * - the incident remains fully functional with {@code ai_summary} staying
 * {@code null} until a summary is successfully generated.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class AiSummaryService {
    private final PromptBuilder promptBuilder;
    private final LlmProvider llmProvider;
    private final IncidentServiceClient incidentClient;

    /**
     * Generates and stores an AI summary for the given incident.
     *
     * <p>
     * Builds a structured prompt from {@code request}, calls the active
     * LLM provider, and writes the result back to the Incident Service.
     * Failures are caught and logged without re-throwing - callers are not
     * affected by LLM unavailability.
     *
     * @param request the incident context sent by the Incident Service
     */
    public void generateSummary(SummaryRequest request) {
        try {
            log.info("Generating AI summary for incident {}", request.getIncidentId());

            String prompt = promptBuilder.build(request);
            String summary = llmProvider.generateSummary(prompt);

            incidentClient.updateAiSummary(request.getIncidentId(), summary);

            log.info("AI summary stored for incident {}", request.getIncidentId());
        } catch (Exception e) {
            log.error("Failed to generate AI summary for incident {}: {}",
                    request.getIncidentId(), e.getMessage(), e);
        }
    }
}
