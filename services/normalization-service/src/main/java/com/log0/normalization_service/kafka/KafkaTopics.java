package com.log0.normalization_service.kafka;

public class KafkaTopics {
    private KafkaTopics() {}

    public static final String RAW_LOGS = "raw-logs";
    public static final String NORMALIZED_LOGS = "normalized-logs";
    public static final String RAW_LOGS_DLQ = "raw-logs-dlq";
}
