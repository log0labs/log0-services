-- Least-privilege account for ai-service. Idempotent: safe to run repeatedly.
-- Run as the admin user (log0). Change the password for anything but local dev.

CREATE USER IF NOT EXISTS ai_reader
    IDENTIFIED WITH sha256_password BY 'ai_reader_dev'
    SETTINGS
        readonly = 2 CONST,
        max_execution_time = 10 MAX 10,
        max_result_rows = 10000 MAX 10000,
        result_overflow_mode = 'throw' CONST;

GRANT SELECT ON log0.log_events TO ai_reader;
