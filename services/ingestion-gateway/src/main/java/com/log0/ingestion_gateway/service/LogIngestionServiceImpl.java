package com.log0.ingestion_gateway.service;

import java.time.Instant;
import java.util.UUID;

import org.springframework.stereotype.Service;

import com.log0.ingestion_gateway.context.RequestContext;
import com.log0.ingestion_gateway.dto.LogIngestionRequest;
import com.log0.ingestion_gateway.dto.RawLogEvent;
import com.log0.ingestion_gateway.kafka.producer.RawLogProducer;

@Service
public class LogIngestionServiceImpl implements LogIngestionService {
    private final RawLogProducer rawLogProducer;

    public LogIngestionServiceImpl(RawLogProducer rawLogProducer) {
        this.rawLogProducer = rawLogProducer;
    }

    @Override
    public void ingest(LogIngestionRequest request, RequestContext context) {
        RawLogEvent event = RawLogEvent.builder()
                .eventId(UUID.randomUUID().toString())
                .tenantId(context.getTenantId())
                .serviceName(context.getServiceName())
                .environment(context.getEnvironment())
                .receivedAt(Instant.now())
                .logTimestamp(request.getTimestamp())
                .level(request.getLevel())
                .message(request.getMessage())
                .trace(request.getTrace())
                .build();

        rawLogProducer.publish(event);
    }
}
