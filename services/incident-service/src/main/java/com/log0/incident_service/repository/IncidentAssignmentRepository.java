package com.log0.incident_service.repository;

import com.log0.incident_service.entity.IncidentAssignment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.UUID;

public interface IncidentAssignmentRepository extends JpaRepository<IncidentAssignment, UUID> {
}