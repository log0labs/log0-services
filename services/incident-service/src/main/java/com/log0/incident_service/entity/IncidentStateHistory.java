package com.log0.incident_service.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.Setter;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "incident_state_history")
@Getter
@Setter
public class IncidentStateHistory {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @Column(name = "history_id")
    private UUID historyId;

    @Column(name = "incident_id", nullable = false)
    private UUID incidentId;

    @Column(name = "from_status")
    private String fromStatus;

    @Column(name = "to_status", nullable = false)
    private String toStatus;

    @Column(name = "changed_by_user_id")
    private UUID changedByUserId;

    @Column(name = "changed_at", nullable = false, updatable = false)
    private Instant changedAt;

    @PrePersist
    void onCreate() {
        changedAt = Instant.now();
    }
}