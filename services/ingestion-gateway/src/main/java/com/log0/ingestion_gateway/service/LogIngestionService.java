package com.log0.ingestion_gateway.service;

import com.log0.ingestion_gateway.context.RequestContext;
import com.log0.ingestion_gateway.dto.LogIngestionRequest;

/**
 * Entry-point contract for the log ingestion business logic.
 * Implementations are expected to enrich the raw request with identity metadata
 * from {@code context} and hand the resulting event off to a Kafka producer.
 */
public interface LogIngestionService {

    /**
     * Validates, enriches, and asynchronously publishes the incoming log event.
     *
     * @param logRequest the raw log payload received from the caller
     * @param context    request-scoped identity metadata (tenant, service, environment)
     */
    public void ingest(
            LogIngestionRequest logRequest,
            RequestContext context);
}
