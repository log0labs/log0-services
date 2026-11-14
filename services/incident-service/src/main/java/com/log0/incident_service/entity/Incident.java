package com.log0.incident_service.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.Setter;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

/**
 * JPA entity representing a grouped incident record in the {@code incident} table.
 * A single incident aggregates repeated occurrences of the same error fingerprint
 * within a tenant, tracking its lifecycle from {@code NEW} through to {@code RESOLVED}.
 * The {@code topMessages} field is stored as {@code jsonb} and holds a sample of
 * representative log messages for the incident.
 */
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

    /** When a summary was last asked for; null for incidents created before this was tracked. */
    @Column(name = "ai_summary_requested_at")
    private Instant aiSummaryRequestedAt;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    /** How long a requested summary is shown as "generating" before it counts as failed. */
    public static final java.time.Duration SUMMARY_PENDING_WINDOW = java.time.Duration.ofMinutes(3);

    /**
     * Where the AI summary stands: {@code READY} when there is text, {@code PENDING} while a request
     * younger than {@link #SUMMARY_PENDING_WINDOW} is in flight, otherwise {@code UNAVAILABLE}
     * (the request failed, was never made, or the model was down).
     */
    public String summaryStatus(Instant now) {
        if (aiSummary != null && !aiSummary.isBlank()) {
            return "READY";
        }
        if (aiSummaryRequestedAt != null && aiSummaryRequestedAt.plus(SUMMARY_PENDING_WINDOW).isAfter(now)) {
            return "PENDING";
        }
        return "UNAVAILABLE";
    }

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