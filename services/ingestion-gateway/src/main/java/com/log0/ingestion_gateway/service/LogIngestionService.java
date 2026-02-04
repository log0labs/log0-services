package com.log0.ingestion_gateway.service;

import com.log0.ingestion_gateway.dto.LogIngestionRequest;
import jakarta.validation.Valid;

public interface LogIngestionService {
    public void ingest(
            String tenantId,
            String serviceName,
            String environment,
            String apiKey,
            LogIngestionRequest logRequest
    );
}
