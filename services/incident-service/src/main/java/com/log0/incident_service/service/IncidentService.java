package com.log0.incident_service.service;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.log0.incident_service.async.AiSummarizer;
import com.log0.incident_service.clickhouse.LogEventRepository;
import com.log0.incident_service.dto.AssignmentDto;
import com.log0.incident_service.dto.IncidentDetailResponse;
import com.log0.incident_service.dto.IncidentEvent;
import com.log0.incident_service.dto.IncidentLogDto;
import com.log0.incident_service.dto.IncidentLogsResponse;
import com.log0.incident_service.dto.NotificationEvent;
import com.log0.incident_service.dto.StatusHistoryDto;
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
    private final AiSummarizer aiSummarizer;
    private final LogEventRepository logEventRepository;

    /**
     * Idempotent upsert keyed on (tenant, fingerprint) for active incidents: refreshes the
     * occurrence count, timestamps, and message samples on an existing active incident, or
     * creates a new one in status {@code NEW} and emits an {@code INCIDENT_CREATED} notification.
     * Resolved incidents are excluded from the lookup so that recurring errors after resolution
     * open a fresh incident.
     *
     * <p>The occurrence count is read from ClickHouse (the source of truth) rather than
     * accumulated from the event, so the operation is fully idempotent: redelivered or duplicate
     * events cannot inflate the count. {@code Math.max} guards against a transient ClickHouse
     * read lagging behind the cached value.
     *
     * @param event the inbound event carrying fingerprint and metadata (its occurrenceCount is
     *              only a fallback used when ClickHouse is unavailable)
     */
    @Transactional
    public void createOrUpdateIncident(IncidentEvent event) {
        UUID tenantId = UUID.fromString(event.getTenantId());
        long liveCount = logEventRepository.countByTenantIdAndFingerprint(tenantId, event.getFingerprint());

        Optional<Incident> existing = incidentRepository.findByTenantIdAndFingerprintAndStatusNot(
                tenantId, event.getFingerprint(), "RESOLVED");

        if (existing.isPresent()) {
            Incident incident = existing.get();
            incident.setOccurrenceCount(Math.max(liveCount, incident.getOccurrenceCount()));
            incident.setLastSeenAt(event.getLastSeenAt());
            incident.setTopMessages(event.getTopMessages());
            incidentRepository.save(incident);
        } else {
            Incident incident = new Incident();
            incident.setTenantId(tenantId);
            incident.setFingerprint(event.getFingerprint());
            incident.setServiceName(event.getServiceName());
            incident.setEnvironment(event.getEnvironment());
            incident.setSeverity(event.getSeverity());
            incident.setStatus("NEW");
            incident.setOccurrenceCount(liveCount > 0 ? liveCount : event.getOccurrenceCount());
            incident.setFirstSeenAt(event.getFirstSeenAt());
            incident.setLastSeenAt(event.getLastSeenAt());
            incident.setTopMessages(event.getTopMessages());
            incidentRepository.save(incident);

            recordHistory(incident.getIncidentId(), null, "NEW", null);

            aiSummarizer.requestSummary(incident);

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
    @Transactional(readOnly = true)
    public Incident getIncident(UUID incidentId, UUID tenantId) {
        Incident incident = incidentRepository.findByIncidentIdAndTenantId(incidentId, tenantId)
                .orElseThrow(() -> new IllegalArgumentException("Incident not found: " + incidentId));
        long liveCount = logEventRepository.countByTenantIdAndFingerprint(tenantId, incident.getFingerprint());
        if (liveCount > 0) {
            incident.setOccurrenceCount(liveCount);
        }
        return incident;
    }

    /**
     * Returns the full incident detail for {@code GET /api/v1/incidents/{id}}: the incident (with
     * its occurrence count refreshed from ClickHouse) plus the current assignment and the ordered
     * status-history timeline. The console relies on the {@code assignment} and {@code statusHistory}
     * fields to render the assignee and timeline and to gate engineer acknowledge/resolve actions.
     */
    @Transactional(readOnly = true)
    public IncidentDetailResponse getIncidentDetail(UUID incidentId, UUID tenantId) {
        Incident incident = getIncident(incidentId, tenantId);

        AssignmentDto assignment = assignmentRepository
                .findFirstByIncidentIdOrderByAssignedAtDesc(incidentId)
                .map(AssignmentDto::from)
                .orElse(null);

        List<StatusHistoryDto> statusHistory = stateHistoryRepository
                .findByIncidentIdOrderByChangedAtAsc(incidentId)
                .stream()
                .map(StatusHistoryDto::from)
                .toList();

        return IncidentDetailResponse.from(incident, assignment, statusHistory);
    }

    /**
     * Persists the AI-generated summary text onto the incident. Called by
     * {@link com.log0.incident_service.controller.IncidentController} when
     * the AI Summary Service delivers a completed summary via its callback.
     *
     * @param incidentId the incident to update
     * @param aiSummary  the generated summary text from the LLM
     * @throws IllegalArgumentException if no incident exists with the given ID
     */
    @Transactional
    public void updateAiSummary(UUID incidentId, String aiSummary) {
        Incident incident = incidentRepository.findById(incidentId)
                .orElseThrow(() -> new IllegalArgumentException("Incident not found: " + incidentId));
        incident.setAiSummary(aiSummary);
        incidentRepository.save(incident);
    }

    /**
     * Returns a page of raw log events that contributed to the given incident,
     * ordered newest-first. The incident is fetched first to resolve its fingerprint
     * and to enforce tenant isolation - a call with a mismatched tenant throws 404
     * before any ClickHouse query is made.
     *
     * <p>
     * Pagination uses {@code page} (0-based) and {@code size}; defaults to
     * page 0 with 50 rows when not supplied by the caller.
     *
     * @param incidentId the incident whose logs to retrieve
     * @param tenantId   the tenant performing the request
     * @param page       0-based page index
     * @param size       maximum number of log events per page (capped at 200)
     * @return paged log events newest-first; empty content if ClickHouse is unavailable
     */
    @Transactional(readOnly = true)
    public IncidentLogsResponse getLogsForIncident(UUID incidentId, UUID tenantId, int page, int size) {
        Incident incident = getIncident(incidentId, tenantId);
        int effectiveSize = Math.min(size, 200);
        int offset = page * effectiveSize;

        List<IncidentLogDto> content = logEventRepository
                .findByTenantIdAndFingerprint(tenantId, incident.getFingerprint(), effectiveSize, offset)
                .stream()
                .map(IncidentLogDto::from)
                .toList();

        long total = logEventRepository.countByTenantIdAndFingerprint(tenantId, incident.getFingerprint());
        int totalPages = effectiveSize == 0 ? 0 : (int) Math.ceil((double) total / effectiveSize);

        return new IncidentLogsResponse(content, total, totalPages, page);
    }

    /**
     * Returns a paginated view of all incidents belonging to the given tenant, with each
     * incident's occurrence count refreshed from ClickHouse (source of truth) in a single
     * batch query. Read-only transaction so the overlaid counts are not flushed back as
     * UPDATEs on every list call.
     */
    @Transactional(readOnly = true)
    public Page<Incident> listIncidents(UUID tenantId, Pageable pageable) {
        Page<Incident> page = incidentRepository.findByTenantId(tenantId, pageable);

        List<String> fingerprints = page.getContent().stream()
                .map(Incident::getFingerprint)
                .distinct()
                .toList();
        Map<String, Long> liveCounts = logEventRepository.countByTenantIdAndFingerprints(tenantId, fingerprints);

        for (Incident incident : page.getContent()) {
            Long liveCount = liveCounts.get(incident.getFingerprint());
            if (liveCount != null && liveCount > 0) {
                incident.setOccurrenceCount(liveCount);
            }
        }

        return page;
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
