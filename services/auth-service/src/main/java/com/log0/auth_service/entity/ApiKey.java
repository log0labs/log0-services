package com.log0.auth_service.entity;

import java.time.Instant;
import java.util.UUID;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.FetchType;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.PrePersist;
import jakarta.persistence.Table;
import lombok.Getter;
import lombok.Setter;

/**
 * JPA entity representing a tenant API key in the {@code api_key} table.
 * API keys are used by services (e.g. the ingestion gateway) to authenticate
 * machine-to-machine requests on behalf of a tenant.
 *
 * <p>
 * Only the SHA-256 hash of the raw key is stored - the plaintext key is shown
 * to the user exactly once at creation time and is never persisted. Validation
 * is performed by hashing the incoming key and comparing against
 * {@code keyHash}.
 */
@Entity
@Table(name = "api_key")
@Getter
@Setter
public class ApiKey {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @Column(name = "key_id")
    private UUID keyId;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "tenant_id", nullable = false)
    private Tenant tenant;

    /** Human-readable label for this key (e.g. "Production Ingestor"). */
    @Column(name = "name", nullable = false)
    private String name;

    /**
     * SHA-256 hash of the raw API key. The raw key is never stored.
     * Validation: hash the incoming key, compare to this value.
     */
    @Column(name = "key_hash", nullable = false, unique = true)
    private String keyHash;

    /**
     * Whether this key is currently active.
     * Setting this to {@code false} immediately revokes access without
     * deleting the record.
     */
    @Column(name = "active", nullable = false)
    private boolean active = true;

    /** Timestamp of the most recent successful use of this key. Nullable. */
    @Column(name = "last_used_at")
    private Instant lastUsedAt;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @PrePersist
    void onCreate() {
        createdAt = Instant.now();
    }
}