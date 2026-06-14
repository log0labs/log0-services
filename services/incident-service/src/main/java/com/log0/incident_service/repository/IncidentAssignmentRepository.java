package com.log0.incident_service.repository;

import com.log0.incident_service.entity.IncidentAssignment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;
import java.util.UUID;

/**
 * Spring Data JPA repository for {@link com.log0.incident_service.entity.IncidentAssignment}.
 * Provides standard CRUD operations; assignment records are append-only by convention.
 */
public interface IncidentAssignmentRepository extends JpaRepository<IncidentAssignment, UUID> {

    /**
     * Returns the most recent assignment for an incident, or empty if it has never been assigned.
     * Because reassignment appends a new row, the latest row by {@code assignedAt} is the
     * effective current assignee.
     */
    Optional<IncidentAssignment> findFirstByIncidentIdOrderByAssignedAtDesc(UUID incidentId);
}