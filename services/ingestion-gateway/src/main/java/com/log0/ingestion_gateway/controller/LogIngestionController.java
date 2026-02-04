package com.log0.ingestion_gateway.controller;

import com.log0.ingestion_gateway.constant.HeaderConstants;
import com.log0.ingestion_gateway.dto.LogIngestionRequest;
import com.log0.ingestion_gateway.service.LogIngestionService;
import com.log0.ingestion_gateway.utils.RequestHeaderExtractor;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;

public class LogIngestionController {
    private final LogIngestionService logIngestionService;

    public LogIngestionController(LogIngestionService logIngestionService) {
        this.logIngestionService = logIngestionService;
    }

    @PostMapping
    public ResponseEntity<Void> ingestLog(
            HttpServletRequest request,
            @Valid @RequestBody LogIngestionRequest logRequest
            ) {
        String tenantId = RequestHeaderExtractor.getRequiredHeader(request, HeaderConstants.TENANT_ID);

        String serviceName = RequestHeaderExtractor.getRequiredHeader(request, HeaderConstants.SERVICE_NAME);

        String environment = RequestHeaderExtractor.getRequiredHeader(request, HeaderConstants.ENVIRONMENT);

        String apiKey = RequestHeaderExtractor.getRequiredHeader(request, HeaderConstants.API_KEY);

        logIngestionService.ingest(tenantId, serviceName, environment, apiKey, logRequest);

        return ResponseEntity.accepted().build();
    }
}
