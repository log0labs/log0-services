package com.log0.incident_service.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.Setter;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

@Entity
@Table(name = "incident")
@Getter
@Setter
public class Incident {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @Column(name = "incident_id")
    private UUID incidentId;

    @Column(name = "tenant_id", nullable = false)
    private UUID tenantId;

    @Column(name = "fingerprint", nullable = false)
    private String fingerprint;

    @Column(name = "service_name", nullable = false)
    private String serviceName;

    @Column(name = "environment", nullable = false)
    private String environment;

    @Column(name = "severity", nullable = false)
    private String severity;

    @Column(name = "status", nullable = false)
    private String status;

    @Column(name = "occurrence_count", nullable = false)
    private Long occurrenceCount;

    @Column(name = "first_seen_at", nullable = false)
    private Instant firstSeenAt;

    @Column(name = "last_seen_at", nullable = false)
    private Instant lastSeenAt;

    @Column(name = "resolved_at")
    private Instant resolvedAt;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "top_messages", columnDefinition = "jsonb")
    private List<String> topMessages;

    @Column(name = "ai_summary", columnDefinition = "text")
    private String aiSummary;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    @PrePersist
    void onCreate() {
        createdAt = Instant.now();
        updatedAt = Instant.now();
    }

    @PreUpdate
    void onUpdate() {
        updatedAt = Instant.now();
    }
}