package com.log0.incident_service.repository;

import com.log0.incident_service.entity.Incident;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;
import java.util.UUID;

/**
 * Spring Data JPA repository for {@link com.log0.incident_service.entity.Incident}.
 * Provides the core lookup queries needed by the incident creation/update flow and
 * the tenant-scoped read APIs.
 */
public interface IncidentRepository extends JpaRepository<Incident, UUID> {

    /**
     * Finds an active (non-resolved) incident matching the given tenant and fingerprint,
     * used by the upsert logic to decide whether to create a new incident or increment
     * an existing one's occurrence count.
     *
     * @param tenantId    the owning tenant
     * @param fingerprint the error fingerprint identifying the incident group
     * @param status      the status value to exclude; callers pass {@code "RESOLVED"}
     * @return the matching incident, or empty if none exists outside the excluded status
     */
    Optional<Incident> findByTenantIdAndFingerprintAndStatusNot(
            UUID tenantId, String fingerprint, String status);

    Page<Incident> findByTenantId(UUID tenantId, Pageable pageable);

    /**
     * Fetches an incident by its ID scoped to a specific tenant, preventing cross-tenant
     * data access.
     */
    Optional<Incident> findByIncidentIdAndTenantId(UUID incidentId, UUID tenantId);
}