package com.log0.ingestion_gateway.kafka;

/**
 * Single source of truth for Kafka topic names used by the ingestion gateway.
 * {@code raw-logs} carries every accepted log event; {@code raw-logs-dlq} receives
 * events that could not be delivered to the primary topic.
 */
public final class KafkaTopics {
    private KafkaTopics() {
    }

    public static final String RAW_LOGS = "raw-logs";
    public static final String RAW_LOGS_DLQ = "raw-logs-dlq";
}
