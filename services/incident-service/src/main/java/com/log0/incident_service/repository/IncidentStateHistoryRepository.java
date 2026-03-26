package com.log0.incident_service.repository;

import com.log0.incident_service.entity.IncidentStateHistory;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.UUID;

public interface IncidentStateHistoryRepository extends JpaRepository<IncidentStateHistory, UUID> {
}