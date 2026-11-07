-- Least-privilege account for ai-service. Idempotent.
-- Run as the admin user (log0). __AI_READER_PASSWORD__ is replaced when you apply it
-- (see the apply command); never commit a real value here.

CREATE USER IF NOT EXISTS ai_reader
    IDENTIFIED WITH sha256_password BY '__AI_READER_PASSWORD__'
    SETTINGS
        readonly = 2 CONST,
        max_execution_time = 10 MAX 10,
        max_result_rows = 10000 MAX 10000,
        result_overflow_mode = 'throw' CONST;

ALTER USER ai_reader IDENTIFIED WITH sha256_password BY '__AI_READER_PASSWORD__';

GRANT SELECT ON log0.log_events TO ai_reader;
