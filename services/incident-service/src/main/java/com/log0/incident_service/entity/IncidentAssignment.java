package com.log0.incident_service.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.Setter;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "incident_assignment")
@Getter
@Setter
public class IncidentAssignment {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @Column(name = "assignment_id")
    private UUID assignmentId;

    @Column(name = "incident_id", nullable = false)
    private UUID incidentId;

    @Column(name = "assigned_to_user_id", nullable = false)
    private UUID assignedToUserId;

    @Column(name = "assigned_by_user_id", nullable = false)
    private UUID assignedByUserId;

    @Column(name = "assigned_at", nullable = false, updatable = false)
    private Instant assignedAt;

    @Column(name = "notes", columnDefinition = "text")
    private String notes;

    @PrePersist
    void onCreate() {
        assignedAt = Instant.now();
    }
}