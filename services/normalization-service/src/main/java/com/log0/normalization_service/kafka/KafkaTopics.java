package com.log0.normalization_service.kafka;

/**
 * Central registry of Kafka topic name constants used across the normalization service.
 *
 * All producers and consumers must reference these constants rather than inlining
 * string literals, so a topic rename is a single-location change.
 */
public class KafkaTopics {
    private KafkaTopics() {
    }

    /** Inbound topic: raw, unvalidated log events ingested from upstream collectors. */
    public static final String RAW_LOGS = "raw-logs";

    /** Outbound topic: successfully normalized and fingerprinted log events. */
    public static final String NORMALIZED_LOGS = "normalized-logs";

    /** Dead-letter topic: raw events that failed normalization, preserved for reprocessing or alerting. */
    public static final String RAW_LOGS_DLQ = "raw-logs-dlq";
}
