-- V1: Create incident management tables
-- Flyway runs this once on first startup. Never edit this file after deploy.
-- To change schema, add a new V2__... migration file.

CREATE TABLE incident (
    incident_id       UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID          NOT NULL,
    fingerprint       VARCHAR(64)   NOT NULL,
    service_name      VARCHAR(255)  NOT NULL,
    environment       VARCHAR(100)  NOT NULL,
    severity          VARCHAR(20)   NOT NULL,
    status            VARCHAR(20)   NOT NULL DEFAULT 'NEW',
    occurrence_count  BIGINT        NOT NULL DEFAULT 1,
    first_seen_at     TIMESTAMPTZ   NOT NULL,
    last_seen_at      TIMESTAMPTZ   NOT NULL,
    resolved_at       TIMESTAMPTZ,
    top_messages      JSONB,
    ai_summary        TEXT,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Index for the core dedup lookup: find open incident by fingerprint + tenant
CREATE INDEX idx_incident_fingerprint_tenant
    ON incident (tenant_id, fingerprint)
    WHERE status != 'RESOLVED';

-- Index for the list API: tenant's incidents ordered by recency
CREATE INDEX idx_incident_tenant_last_seen
    ON incident (tenant_id, last_seen_at DESC);

CREATE TABLE incident_assignment (
    assignment_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id          UUID         NOT NULL REFERENCES incident(incident_id),
    assigned_to_user_id  UUID         NOT NULL,
    assigned_by_user_id  UUID         NOT NULL,
    assigned_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    notes                TEXT
);

CREATE INDEX idx_assignment_incident
    ON incident_assignment (incident_id, assigned_at DESC);

CREATE TABLE incident_state_history (
    history_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id         UUID         NOT NULL REFERENCES incident(incident_id),
    from_status         VARCHAR(20),
    to_status           VARCHAR(20)  NOT NULL,
    changed_by_user_id  UUID,
    changed_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_state_history_incident
    ON incident_state_history (incident_id, changed_at DESC);
