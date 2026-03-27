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

@Service
@RequiredArgsConstructor
public class IncidentService {
    private final IncidentRepository incidentRepository;
    private final IncidentAssignmentRepository assignmentRepository;
    private final IncidentStateHistoryRepository stateHistoryRepository;
    private final IncidentStateMachine stateMachine;
    private final NotificationEventPublisher notificationPublisher;

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

    @Transactional
    public void acknowledgeIncident(UUID incidentId, UUID tenantId, UUID userId) {
        Incident incident = getIncident(incidentId, tenantId);
        stateMachine.transition(incident.getStatus(), "ACKNOWLEDGED");

        String previousStatus = incident.getStatus();
        incident.setStatus("ACKNOWLEDGED");
        incidentRepository.save(incident);

        recordHistory(incidentId, previousStatus, "ACKNOWLEDGED", userId);
    }

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

    public Incident getIncident(UUID incidentId, UUID tenantId) {
        return incidentRepository.findByIncidentIdAndTenantId(incidentId, tenantId)
                .orElseThrow(() -> new IllegalArgumentException("Incident not found: " + incidentId));
    }

    public Page<Incident> listIncidents(UUID tenantId, Pageable pageable) {
        return incidentRepository.findByTenantId(tenantId, pageable);
    }

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
