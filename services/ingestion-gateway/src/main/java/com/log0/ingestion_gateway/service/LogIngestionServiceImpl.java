package com.log0.ingestion_gateway.service;

import com.log0.ingestion_gateway.dto.LogIngestionRequest;
import com.log0.ingestion_gateway.dto.RawLogEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Instant;
import java.util.UUID;

public class LogIngestionServiceImpl implements LogIngestionService {
    private static final Logger logger = LoggerFactory.getLogger(LogIngestionServiceImpl.class);

    @Override
    public void ingest(
            String tenantId,
            String serviceName,
            String environment,
            String apiKey,
            LogIngestionRequest request
    ) {
        RawLogEvent event = new RawLogEvent();
        event.setEventId(UUID.randomUUID().toString());
        event.setTenantId(tenantId);
        event.setServiceName(serviceName);
        event.setEnvironment(environment);

        event.setReceivedAt(Instant.now());
        event.setLogTimestamp(request.getTimestamp());

        event.setLevel(request.getLevel());
        event.setMessage(request.getMessage());
        event.setTrace(request.getTrace());

        // Temporary: log instead of publishing to Kafka
        logger.info(
                "Ingested log event [eventId={}, tenant={}, service={}, env={}, level={}]",
                event.getEventId(),
                tenantId,
                serviceName,
                environment,
                event.getLevel()
        );
    }
}
