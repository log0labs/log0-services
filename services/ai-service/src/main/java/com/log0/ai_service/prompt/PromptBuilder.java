package com.log0.ai_service.prompt;

import org.springframework.stereotype.Component;

import com.log0.ai_service.dto.SummaryRequest;

/**
 * Builds the user-facing prompt text sent to the LLM from a
 * {@link SummaryRequest}.
 *
 * <p>
 * Formats incident context - service, severity, occurrence count, time range,
 * and top error messages - into a structured plain-text block. The system
 * prompt (which instructs the model on response format) lives in each
 * {@link com.log0.ai_service.provider.LlmProvider} implementation. This class
 * only builds the user content: the raw incident data.
 *
 * <p>
 * Keeping prompt construction separate from the providers means the prompt
 * format can be improved without touching any provider code.
 */
@Component
public class PromptBuilder {
    /**
     * Formats all incident fields from {@code request} into a structured
     * plain-text prompt ready to send to the LLM as the user message.
     *
     * @param request the incident context received from the Incident Service
     * @return a formatted multi-line string describing the incident
     */
    public String build(SummaryRequest request) {
        StringBuilder messages = new StringBuilder();
        for (String message : request.getTopMessages()) {
            messages.append(" - \"").append(message).append("\"\n");
        }

        return """
                Service: %s (%s)
                Severity: %s
                Occurrences: %d
                First seen: %s
                Last seen: %s
                Top error messages:
                %s
                """.formatted(
                request.getServiceName(),
                request.getEnvironment(),
                request.getSeverity(),
                request.getOccurrenceCount(),
                request.getFirstSeenAt(),
                request.getLastSeenAt(),
                messages.toString().stripTrailing());
    }
}
