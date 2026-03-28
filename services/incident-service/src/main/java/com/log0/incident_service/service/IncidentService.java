package com.log0.incident_service.service;

import java.time.Instant;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.log0.incident_service.dto.IncidentEvent;
import com.log0.incident_service.dto.NotificationEvent;
import com.log0.incident_service.entity.Incident;
import com.log0.incident_service.entity.IncidentAssignment;
import com.log0.incident_service.entity.IncidentStateHistory;
import com.log0.incident_service.kafka.producer.NotificationEventPublisher;
import com.log0.incident_service.repository.IncidentAssignmentRepository;
import com.log0.incident_service.repository.IncidentRepository;
import com.log0.incident_service.repository.IncidentStateHistoryRepository;
import com.log0.incident_service.statemachine.IncidentStateMachine;

import lombok.RequiredArgsConstructor;

/**
 * Core business logic for the incident lifecycle, covering creation, state transitions,
 * assignment, and resolution. Every mutating method is transactional and enforces
 * allowed transitions through {@link IncidentStateMachine} before persisting changes.
 * Downstream systems are notified of significant transitions via
 * {@link NotificationEventPublisher} on the {@code notification-events} topic.
 */
@Service
@RequiredArgsConstructor
public class IncidentService {
    private final IncidentRepository incidentRepository;
    private final IncidentAssignmentRepository assignmentRepository;
    private final IncidentStateHistoryRepository stateHistoryRepository;
    private final IncidentStateMachine stateMachine;
    private final NotificationEventPublisher notificationPublisher;

    /**
     * Idempotent upsert: increments occurrence count and refreshes timestamps on an
     * existing active incident with a matching fingerprint, or creates a new one in
     * status {@code NEW} and emits an {@code INCIDENT_CREATED} notification.
     * Resolved incidents are excluded from the lookup so that recurring errors after
     * resolution open a fresh incident.
     *
     * @param event the inbound event carrying fingerprint, occurrence data, and metadata
     */
    @Transactional
    public void createOrUpdateIncident(IncidentEvent event) {
        Optional<Incident> existing = incidentRepository.findByTenantIdAndFingerprintAndStatusNot(
                UUID.fromString(event.getTenantId()),
                event.getFingerprint(),
                "RESOLVED");

        if (existing.isPresent()) {
            Incident incident = existing.get();
            incident.setOccurrenceCount(incident.getOccurrenceCount() + event.getOccurrenceCount());
            incident.setLastSeenAt(event.getLastSeenAt());
            incident.setTopMessages(event.getTopMessages());
            incidentRepository.save(incident);
        } else {
            Incident incident = new Incident();
            incident.setTenantId(UUID.fromString(event.getTenantId()));
            incident.setFingerprint(event.getFingerprint());
            incident.setServiceName(event.getServiceName());
            incident.setEnvironment(event.getEnvironment());
            incident.setSeverity(event.getSeverity());
            incident.setStatus("NEW");
            incident.setOccurrenceCount(event.getOccurrenceCount());
            incident.setFirstSeenAt(event.getFirstSeenAt());
            incident.setLastSeenAt(event.getLastSeenAt());
            incident.setTopMessages(event.getTopMessages());
            incidentRepository.save(incident);

            recordHistory(incident.getIncidentId(), null, "NEW", null);

            // TODO [Phase 6]: trigger AISummaryService.generateSummary(incident) async here

            notificationPublisher.publish(buildNotificationEvent(incident, "INCIDENT_CREATED", null));
        }
    }

    /**
     * Transitions the incident to {@code ASSIGNED}, persists an {@link IncidentAssignment}
     * record, appends a state history entry, and emits an {@code INCIDENT_ASSIGNED}
     * notification targeted at the assignee.
     *
     * @param assignedToUserId the user who will own the incident
     * @param assignedByUserId the user performing the assignment (recorded for audit)
     * @param notes            optional context for the assignee; may be {@code null}
     * @throws IllegalStateException if the current status does not permit assignment
     */
    @Transactional
    public void assignIncident(UUID incidentId, UUID tenantId, UUID assignedToUserId, UUID assignedByUserId,
            String notes) {
        Incident incident = getIncident(incidentId, tenantId);
        stateMachine.transition(incident.getStatus(), "ASSIGNED");

        String previousStatus = incident.getStatus();
        incident.setStatus("ASSIGNED");
        incidentRepository.save(incident);

        IncidentAssignment assignment = new IncidentAssignment();
        assignment.setIncidentId(incidentId);
        assignment.setAssignedToUserId(assignedToUserId);
        assignment.setAssignedByUserId(assignedByUserId);
        assignment.setNotes(notes);
        assignmentRepository.save(assignment);

        recordHistory(incidentId, previousStatus, "ASSIGNED", assignedByUserId);

        notificationPublisher
                .publish(buildNotificationEvent(incident, "INCIDENT_ASSIGNED", assignedToUserId.toString()));
    }

    /**
     * Transitions the incident to {@code ACKNOWLEDGED} and records the acting user in
     * the state history. No notification is emitted for this transition.
     *
     * @throws IllegalStateException if the current status does not permit acknowledgement
     */
    @Transactional
    public void acknowledgeIncident(UUID incidentId, UUID tenantId, UUID userId) {
        Incident incident = getIncident(incidentId, tenantId);
        stateMachine.transition(incident.getStatus(), "ACKNOWLEDGED");

        String previousStatus = incident.getStatus();
        incident.setStatus("ACKNOWLEDGED");
        incidentRepository.save(incident);

        recordHistory(incidentId, previousStatus, "ACKNOWLEDGED", userId);
    }

    /**
     * Transitions the incident to {@code RESOLVED}, stamps {@code resolvedAt}, appends
     * a state history entry, and emits an {@code INCIDENT_RESOLVED} notification.
     *
     * @throws IllegalStateException if the current status does not permit resolution
     */
    @Transactional
    public void resolveIncident(UUID incidentId, UUID tenantId, UUID userId) {
        Incident incident = getIncident(incidentId, tenantId);
        stateMachine.transition(incident.getStatus(), "RESOLVED");

        String previousStatus = incident.getStatus();
        incident.setStatus("RESOLVED");
        incident.setResolvedAt(Instant.now());
        incidentRepository.save(incident);

        recordHistory(incidentId, previousStatus, "RESOLVED", userId);

        notificationPublisher.publish(buildNotificationEvent(incident, "INCIDENT_RESOLVED", null));
    }

    /**
     * Fetches a tenant-scoped incident, throwing {@link IllegalArgumentException} (mapped
     * to HTTP 404) if no matching record exists. Also used internally by all mutating
     * methods to enforce tenant isolation before any state change.
     */
    public Incident getIncident(UUID incidentId, UUID tenantId) {
        return incidentRepository.findByIncidentIdAndTenantId(incidentId, tenantId)
                .orElseThrow(() -> new IllegalArgumentException("Incident not found: " + incidentId));
    }

    /** Returns a paginated view of all incidents belonging to the given tenant. */
    public Page<Incident> listIncidents(UUID tenantId, Pageable pageable) {
        return incidentRepository.findByTenantId(tenantId, pageable);
    }

    /**
     * Appends an immutable state-history row for every status transition.
     *
     * @param fromStatus      the previous status; {@code null} for the initial {@code NEW} entry
     * @param changedByUserId the acting user; {@code null} for system-driven transitions
     */
    private void recordHistory(UUID incidentId, String fromStatus, String toStatus, UUID changedByUserId) {
        IncidentStateHistory history = new IncidentStateHistory();
        history.setIncidentId(incidentId);
        history.setFromStatus(fromStatus);
        history.setToStatus(toStatus);
        history.setChangedByUserId(changedByUserId);
        stateHistoryRepository.save(history);
    }

    private NotificationEvent buildNotificationEvent(Incident incident, String notificationType,
            String assignedToUserId) {
        return NotificationEvent.builder()
                .tenantId(incident.getTenantId().toString())
                .incidentId(incident.getIncidentId().toString())
                .fingerprint(incident.getFingerprint())
                .serviceName(incident.getServiceName())
                .environment(incident.getEnvironment())
                .severity(incident.getSeverity())
                .status(incident.getStatus())
                .occurrenceCount(incident.getOccurrenceCount())
                .firstSeenAt(incident.getFirstSeenAt())
                .lastSeenAt(incident.getLastSeenAt())
                .topMessages(incident.getTopMessages())
                .aiSummary(incident.getAiSummary())
                .notificationType(notificationType)
                .assignedToUserId(assignedToUserId)
                .build();
    }
}
