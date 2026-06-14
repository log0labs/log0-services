-- V2: Enforce a single active (non-resolved) incident per (tenant, fingerprint).
-- Replaces the non-unique lookup index from V1 with a partial UNIQUE index, so duplicate
-- active incidents for the same fingerprint can never be created - a defense against
-- concurrent incident-event processing racing on the find-then-create path.
-- Resolved incidents are excluded so a recurring error after resolution can still open a
-- fresh incident.
--
-- NOTE: this migration fails if duplicate active rows already exist. Truncate the incident
-- tables before first applying it (the pre-fix data had inflated/duplicate rows).

DROP INDEX IF EXISTS idx_incident_fingerprint_tenant;

CREATE UNIQUE INDEX idx_incident_fingerprint_tenant_active
    ON incident (tenant_id, fingerprint)
    WHERE status != 'RESOLVED';
