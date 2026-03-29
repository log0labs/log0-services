package com.log0.ai_service.controller;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.log0.ai_service.dto.SummaryRequest;
import com.log0.ai_service.service.AiSummaryService;

import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;

/**
 * REST controller exposing the AI summary generation API under
 * {@code /api/v1/summaries}.
 *
 * <p>
 * Receives summary requests from the Incident Service, delegates to
 * {@link AiSummaryService}, and returns {@code 202 Accepted} immediately.
 * The actual LLM call and callback to the Incident Service happen
 * synchronously within the request - the async boundary is on the
 * Incident Service side ({@code @Async}), not here.
 */
@RestController
@RequestMapping("/api/v1/summaries")
@RequiredArgsConstructor
public class SummaryController {
    private final AiSummaryService aiSummaryService;

    /**
     * Accepts an incident summary request and triggers AI summary generation.
     *
     * <p>
     * Validates the request body, calls {@link AiSummaryService#generateSummary},
     * then returns {@code 202 Accepted}. Any LLM or callback failure is caught
     * inside {@code AiSummaryService} and logged - this endpoint always returns
     * {@code 202} as long as the request body is valid.
     *
     * @param request the incident context to summarise; validated before processing
     * @return {@code 202 Accepted} with no body
     */
    @PostMapping
    public ResponseEntity<Void> summarize(@Valid @RequestBody SummaryRequest request) {
        aiSummaryService.generateSummary(request);
        return ResponseEntity.accepted().build();
    }
}
