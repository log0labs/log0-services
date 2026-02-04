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

@RestController
@RequestMapping("/api/v1/logs")
public class LogIngestionController {
    private final LogIngestionService logIngestionService;

    public LogIngestionController(LogIngestionService logIngestionService) {
        this.logIngestionService = logIngestionService;
    }

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
