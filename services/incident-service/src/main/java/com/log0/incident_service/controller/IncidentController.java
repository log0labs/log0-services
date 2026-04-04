package com.log0.incident_service.controller;

import java.util.List;
import java.util.Map;
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

import com.log0.incident_service.clickhouse.LogEventDto;
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

    /**
     * Returns a paginated list of raw log events that contributed to the given incident,
     * ordered newest-first. The incident's fingerprint is used to query ClickHouse;
     * tenant isolation is enforced before the ClickHouse query is made.
     *
     * <p>
     * Defaults to page 0, 50 rows per page. Size is capped at 200 server-side.
     *
     * @param incidentId the incident whose logs to retrieve
     * @param tenantId   the tenant performing the request
     * @param page       0-based page index (default 0)
     * @param size       rows per page (default 50, max 200)
     */
    @GetMapping("/{incidentId}/logs")
    public ResponseEntity<List<LogEventDto>> getLogsForIncident(
            @PathVariable UUID incidentId,
            @RequestParam UUID tenantId,
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "50") int size) {
        return ResponseEntity.ok(incidentService.getLogsForIncident(incidentId, tenantId, page, size));
    }

    /**
     * Receives the AI-generated summary from the AI Summary Service and persists it
     * on the incident. Called as a callback after the LLM completes its analysis.
     * Returns 204 on success.
     *
     * @param incidentId the incident to update
     * @param body       JSON object with a single {@code aiSummary} string field
     */
    @PatchMapping("/{incidentId}/ai-summary")
    public ResponseEntity<Void> updateAiSummary(
            @PathVariable UUID incidentId,
            @RequestBody Map<String, String> body) {
        incidentService.updateAiSummary(incidentId, body.get("aiSummary"));
        return ResponseEntity.noContent().build();
    }
}
