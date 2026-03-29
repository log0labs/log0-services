package com.log0.auth_service.entity;

import java.time.Instant;
import java.util.UUID;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.PrePersist;
import jakarta.persistence.Table;
import lombok.Getter;
import lombok.Setter;

/**
 * JPA entity representing an organisation (tenant) in the {@code tenant} table.
 * Every other entity in auth-service (User, ApiKey) belongs to a tenant.
 * The {@code slug} is a URL-safe lowercase identifier chosen at registration
 * (e.g. {@code acme-corp}) and must be globally unique across all tenants.
 */
@Entity
@Table(name = "tenant")
@Getter
@Setter
public class Tenant {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @Column(name = "tenant_id")
    private UUID tenantId;

    /** Human-readable display name of the organisation (e.g. "Acme Corp"). */
    @Column(name = "name", nullable = false)
    private String name;

    /**
     * URL-safe unique identifier for the tenant (e.g. {@code acme-corp}).
     * Used in API paths and log routing. Immutable after creation.
     */
    @Column(name = "slug", nullable = false, unique = true, updatable = false)
    private String slug;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @PrePersist
    void onCreate() {
        createdAt = Instant.now();
    }
}
