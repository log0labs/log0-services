package com.log0.incident_service.controller;

import java.util.UUID;

import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.web.PageableDefault;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.log0.incident_service.dto.ActorRequest;
import com.log0.incident_service.dto.AssignRequest;
import com.log0.incident_service.entity.Incident;
import com.log0.incident_service.service.IncidentService;

import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;

/**
 * REST controller exposing the incident management API under {@code /api/v1/incidents}.
 * All mutating operations (assign, acknowledge, resolve) are tenant-scoped and delegate
 * state-transition enforcement to {@link com.log0.incident_service.statemachine.IncidentStateMachine}.
 */
@RestController
@RequestMapping("/api/v1/incidents")
@RequiredArgsConstructor
public class IncidentController {
    private final IncidentService incidentService;

    /**
     * Returns a paginated list of incidents for the given tenant, sorted by
     * {@code lastSeenAt} descending by default (page size 20).
     */
    @GetMapping
    public ResponseEntity<Page<Incident>> listIncidents(
            @RequestParam UUID tenantId,
            @PageableDefault(size = 20, sort = "lastSeenAt") Pageable pageable) {
        return ResponseEntity.ok(incidentService.listIncidents(tenantId, pageable));
    }

    /**
     * Returns a single incident scoped to the given tenant.
     * Returns 404 if the incident does not exist or belongs to a different tenant.
     */
    @GetMapping("/{incidentId}")
    public ResponseEntity<Incident> getIncident(@PathVariable UUID incidentId, @RequestParam UUID tenantId) {
        return ResponseEntity.ok(incidentService.getIncident(incidentId, tenantId));
    }

    /**
     * Transitions the incident to {@code ASSIGNED}, persists the assignment record,
     * and triggers an {@code INCIDENT_ASSIGNED} Slack notification to the assignee.
     * Returns 204 on success; 409 if the current status does not permit assignment.
     */
    @PatchMapping("/{incidentId}/assign")
    public ResponseEntity<Void> assignIncident(
            @PathVariable UUID incidentId,
            @RequestParam UUID tenantId,
            @Valid @RequestBody AssignRequest request) {
        incidentService.assignIncident(
                incidentId,
                tenantId,
                request.getAssignedToUserId(),
                request.getAssignedByUserId(),
                request.getNotes());
        return ResponseEntity.noContent().build();
    }

    /**
     * Transitions the incident to {@code ACKNOWLEDGED}.
     * Returns 204 on success; 409 if the current status does not permit acknowledgement.
     * No Slack notification is emitted for this transition.
     */
    @PatchMapping("/{incidentId}/acknowledge")
    public ResponseEntity<Void> acknowledgeIncident(
            @PathVariable UUID incidentId,
            @RequestParam UUID tenantId,
            @Valid @RequestBody ActorRequest request) {
        incidentService.acknowledgeIncident(incidentId, tenantId, request.getUserId());
        return ResponseEntity.noContent().build();
    }

    /**
     * Transitions the incident to {@code RESOLVED} and triggers an {@code INCIDENT_RESOLVED}
     * Slack notification. Returns 204 on success; 409 if the current status does not permit resolution.
     */
    @PatchMapping("/{incidentId}/resolve")
    public ResponseEntity<Void> resolveIncident(
            @PathVariable UUID incidentId,
            @RequestParam UUID tenantId,
            @Valid @RequestBody ActorRequest request) {
        incidentService.resolveIncident(incidentId, tenantId, request.getUserId());
        return ResponseEntity.noContent().build();
    }
}
