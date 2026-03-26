package com.log0.incident_service.repository;

import com.log0.incident_service.entity.Incident;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;
import java.util.UUID;

public interface IncidentRepository extends JpaRepository<Incident, UUID> {

    Optional<Incident> findByTenantIdAndFingerprintAndStatusNot(
            UUID tenantId, String fingerprint, String status);

    Page<Incident> findByTenantId(UUID tenantId, Pageable pageable);

    Optional<Incident> findByIncidentIdAndTenantId(UUID incidentId, UUID tenantId);
}