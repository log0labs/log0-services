package com.log0.normalization_service.processor;

import java.time.Instant;
import java.util.HashMap;
import java.util.Map;

import org.springframework.stereotype.Component;

import com.log0.normalization_service.dto.NormalizedLogEvent;
import com.log0.normalization_service.dto.RawLogEvent;

import lombok.RequiredArgsConstructor;

/**
 * Transforms a {@link RawLogEvent} from the {@code raw-logs} topic into a
 * {@link NormalizedLogEvent} ready for publication to {@code normalized-logs}.
 *
 * Responsibilities include timestamp resolution, level canonicalization, message
 * sanitization, message-template extraction, and fingerprint generation. Attribute
 * extraction is currently a stub pending Phase 3.6.
 */
@Component
@RequiredArgsConstructor
public class LogNormalizer {
    private final FingerprintGenerator fingerprintGenerator;

    /**
     * Resolves the best available timestamp for a log event, falling back from the
     * source-reported time to the ingest time, and finally to wall-clock now.
     *
     * This three-tier fallback ensures every normalized event has a timestamp even
     * when upstream clients omit {@code logTimestamp} or {@code receivedAt}.
     */
    private Instant resolveTimestamp(RawLogEvent raw) {
        return raw.getLogTimestamp() != null
                ? raw.getLogTimestamp()
                : raw.getReceivedAt() != null
                        ? raw.getReceivedAt()
                        : Instant.now();
    }

    /**
     * Canonicalizes a log level to trimmed uppercase, defaulting to {@code "INFO"} when absent.
     */
    private String normalizeLevel(String level) {
        if (level == null)
            return "INFO";
        return level.trim().toUpperCase();
    }

    private String normalizeMessage(String message) {
        return message == null ? "" : message.trim();
    }

    private Map<String, Object> extractAttributes(RawLogEvent raw) {
        // Placeholder for attribute extraction logic
        return new HashMap<>();
    }

    private String buildMessageTemplate(String message) {
        return fingerprintGenerator.buildMessageTemplate(message);
    }

    /**
     * Converts a {@link RawLogEvent} into a fully populated {@link NormalizedLogEvent}.
     *
     * Callers receive an event with a stable fingerprint and message template that are
     * suitable for deduplication by downstream services. The schema version is fixed at
     * {@code "v1"} for this release; {@code exceptionType} is intentionally null until
     * Phase 3.6 (see inline TODO for details).
     *
     * @param raw the inbound event consumed from {@code raw-logs}; must not be null
     * @return a normalized event ready to publish to {@code normalized-logs}
     */
    public NormalizedLogEvent normalize(RawLogEvent raw) {
        String normalizedMessage = normalizeMessage(raw.getMessage());
        String messageTemplate = buildMessageTemplate(normalizedMessage);
        String fingerprint = fingerprintGenerator.generate(
                raw.getServiceName(),
                messageTemplate,
                // TODO: [Phase 3.6] Extract exception type to improve fingerprint accuracy.
                //
                // WHY it matters: exceptionType is the 3rd component of the fingerprint formula:
                //   SHA-256( service | messageTemplate | exceptionType | firstStackFrame )
                // Without it, a TimeoutException and a NullPointerException thrown from the
                // same method produce the same fingerprint → same incident. They should be separate.
                //
                // WHERE to get it - two sources, in priority order:
                //   1. raw.getTrace() first line - standard Java stack traces start with the
                //      exception class before the colon:
                //        "java.lang.NullPointerException: Cannot invoke method charge()"
                //      Parse: split on '\n', take line[0], split on ':', take part[0].trim()
                //      Optionally strip the package prefix to keep it short ("NullPointerException").
                //
                //   2. attributes.get("exception.type") - when clients send structured logs
                //      with OpenTelemetry-style attributes, the exception class will be here.
                //      Check this after attributes extraction is implemented in extractAttributes().
                //
                // WHEN to implement: after extractAttributes() is no longer a stub (Phase 3.6).
                // For now null is safe - FingerprintGenerator.safe() converts it to "" so the
                // hash input becomes "service|template||firstFrame" instead of crashing.
                null,
                raw.getTrace());

        return NormalizedLogEvent.builder()
                .eventId(raw.getEventId())
                .tenantId(raw.getTenantId())
                .serviceName(raw.getServiceName())
                .environment(raw.getEnvironment())
                .timestamp(resolveTimestamp(raw))
                .level(normalizeLevel(raw.getLevel()))
                .message(normalizedMessage)
                .messageTemplate(messageTemplate)
                .fingerprint(fingerprint)
                .attributes(extractAttributes(raw))
                .traceId(raw.getTrace())
                .schemaVersion("v1")
                .build();
    }
}
