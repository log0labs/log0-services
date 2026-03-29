package com.log0.auth_service.repository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;

import com.log0.auth_service.entity.ApiKey;

/**
 * Spring Data JPA repository for {@link ApiKey}.
 * Provides API key lookups by hash (validation path) and by tenant
 * (management path for listing and revoking keys).
 */
public interface ApiKeyRepository extends JpaRepository<ApiKey, UUID> {

    /**
     * Finds an active API key by its SHA-256 hash.
     * Called by the validation endpoint when the ingestion gateway presents a key.
     * Only active keys are returned — revoked keys are ignored.
     *
     * @param keyHash the SHA-256 hash of the raw API key
     * @return the matching active key, or empty if not found or revoked
     */
    Optional<ApiKey> findByKeyHashAndActiveTrue(String keyHash);

    /**
     * Lists all API keys belonging to a tenant.
     * Used by the management API to show a tenant's keys.
     *
     * @param tenantId the tenant whose keys to list
     * @return all keys for the tenant, active and revoked
     */
    List<ApiKey> findByTenant_TenantId(UUID tenantId);
}
