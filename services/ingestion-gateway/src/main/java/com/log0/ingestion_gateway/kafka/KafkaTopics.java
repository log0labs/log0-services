package com.log0.ingestion_gateway.kafka;

public final class KafkaTopics {
    private KafkaTopics() {
    }

    public static final String RAW_LOGS = "raw-logs";
    public static final String RAW_LOGS_DLQ = "raw-logs-dlq";
}
