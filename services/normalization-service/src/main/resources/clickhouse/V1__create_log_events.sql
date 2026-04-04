CREATE TABLE
    IF NOT EXISTS log0.log_events (
        event_id               String,
        tenant_id              String,
        service_name           String,
        environment            String,
        timestamp              DateTime64(3, 'UTC'),
        level                  String,
        message                String,
        message_template       String,
        fingerprint            String,
        trace_id               String,
        schema_version         String
    ) ENGINE = MergeTree ()
ORDER BY
    (tenant_id, timestamp, fingerprint);
