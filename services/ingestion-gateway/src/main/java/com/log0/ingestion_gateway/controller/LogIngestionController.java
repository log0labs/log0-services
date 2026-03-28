package com.log0.ingestion_gateway.controller;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.log0.ingestion_gateway.constant.HeaderConstants;
import com.log0.ingestion_gateway.context.RequestContext;
import com.log0.ingestion_gateway.dto.LogIngestionRequest;
import com.log0.ingestion_gateway.service.LogIngestionService;
import com.log0.ingestion_gateway.utils.RequestHeaderExtractor;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;

/**
 * REST entry point for the log ingestion pipeline, exposed at {@code POST /api/v1/logs}.
 * Extracts the four required identity headers, builds a {@link RequestContext}, and
 * delegates to {@link LogIngestionService}; returns {@code 202 Accepted} on success
 * so callers are not blocked waiting for Kafka acknowledgement.
 */
@RestController
@RequestMapping("/api/v1/logs")
public class LogIngestionController {
    private final LogIngestionService logIngestionService;

    public LogIngestionController(LogIngestionService logIngestionService) {
        this.logIngestionService = logIngestionService;
    }

    /**
     * Accepts a single log event, validates it, and hands it off to the ingestion
     * service for async publication to the {@code raw-logs} Kafka topic.
     * Throws {@link IllegalArgumentException} (mapped to 400) if any required header
     * is absent; bean-validation failures are handled by
     * {@link com.log0.ingestion_gateway.exception.GlobalExceptionHandler}.
     *
     * @param request    the raw servlet request used to read identity headers
     * @param logRequest the validated log payload from the request body
     * @return {@code 202 Accepted} with an empty body
     */
    @PostMapping
    public ResponseEntity<Void> ingestLog(
            HttpServletRequest request,
            @Valid @RequestBody LogIngestionRequest logRequest) {
        String tenantId = RequestHeaderExtractor.getRequiredHeader(request, HeaderConstants.TENANT_ID);
        String serviceName = RequestHeaderExtractor.getRequiredHeader(request, HeaderConstants.SERVICE_NAME);
        String environment = RequestHeaderExtractor.getRequiredHeader(request, HeaderConstants.ENVIRONMENT);
        String apiKey = RequestHeaderExtractor.getRequiredHeader(request, HeaderConstants.API_KEY);

        RequestContext context = new RequestContext(
                tenantId,
                serviceName,
                environment,
                apiKey);

        logIngestionService.ingest(logRequest, context);

        return ResponseEntity.accepted().build();
    }
}
