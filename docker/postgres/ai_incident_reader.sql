-- Read-only role for ai-service to read incident history. Idempotent.
-- Run as the admin user (log0) against database log0, passing the password as a psql
-- variable:   psql ... -v ai_pw=<password>
-- Re-running with a new value rotates the password.

-- Refuse to run with an empty password (an empty value would CLEAR the role's password).
SELECT length(:'ai_pw') = 0 AS empty_pw \gset
\if :empty_pw
  \echo 'ERROR: ai_pw is empty. Pass -v ai_pw=<password> (is INCIDENT_DB_PASSWORD set?)'
  \quit
\endif

SELECT 'CREATE ROLE ai_incident_reader LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ai_incident_reader') \gexec

ALTER ROLE ai_incident_reader PASSWORD :'ai_pw';

GRANT CONNECT ON DATABASE log0 TO ai_incident_reader;
GRANT USAGE ON SCHEMA public TO ai_incident_reader;
GRANT SELECT ON public.incident TO ai_incident_reader;
