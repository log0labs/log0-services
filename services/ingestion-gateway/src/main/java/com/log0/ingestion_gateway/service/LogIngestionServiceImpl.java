package com.log0.ingestion_gateway.service;

import java.time.Instant;
import java.util.UUID;

import org.springframework.stereotype.Service;

import com.log0.ingestion_gateway.context.RequestContext;
import com.log0.ingestion_gateway.dto.LogIngestionRequest;
import com.log0.ingestion_gateway.dto.RawLogEvent;
import com.log0.ingestion_gateway.kafka.producer.RawLogProducer;

/**
 * Implements {@link LogIngestionService} by assembling a {@link RawLogEvent} from the
 * inbound request and request context, then delegating to {@link RawLogProducer} for
 * async delivery to the {@code raw-logs} Kafka topic.
 * A random UUID is assigned as the event ID and the gateway's wall-clock time is
 * stamped as {@code receivedAt} to preserve ingestion ordering independently of the
 * client-supplied log timestamp.
 */
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
