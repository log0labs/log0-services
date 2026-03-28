package com.log0.incident_service.repository;

import com.log0.incident_service.entity.IncidentAssignment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.UUID;

/**
 * Spring Data JPA repository for {@link com.log0.incident_service.entity.IncidentAssignment}.
 * Provides standard CRUD operations; assignment records are append-only by convention.
 */
public interface IncidentAssignmentRepository extends JpaRepository<IncidentAssignment, UUID> {
}