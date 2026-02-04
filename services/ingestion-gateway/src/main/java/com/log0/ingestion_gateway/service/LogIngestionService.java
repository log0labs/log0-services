package com.log0.ingestion_gateway.service;

import com.log0.ingestion_gateway.context.RequestContext;
import com.log0.ingestion_gateway.dto.LogIngestionRequest;

public interface LogIngestionService {
    public void ingest(
            LogIngestionRequest logRequest,
            RequestContext context);
}
