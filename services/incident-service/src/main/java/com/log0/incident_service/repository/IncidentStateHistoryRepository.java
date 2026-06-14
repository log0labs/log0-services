package com.log0.incident_service.repository;

import com.log0.incident_service.entity.IncidentStateHistory;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.UUID;

/**
 * Spring Data JPA repository for {@link com.log0.incident_service.entity.IncidentStateHistory}.
 * Records are written on every status transition and are never updated or deleted,
 * forming an immutable audit log.
 */
public interface IncidentStateHistoryRepository extends JpaRepository<IncidentStateHistory, UUID> {

    /**
     * Returns the full status-transition timeline for an incident, oldest first - the order the
     * console renders the detail timeline in.
     */
    List<IncidentStateHistory> findByIncidentIdOrderByChangedAtAsc(UUID incidentId);
}