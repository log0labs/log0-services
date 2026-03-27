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

@RestController
@RequestMapping("/api/v1/incidents")
@RequiredArgsConstructor
public class IncidentController {
    private final IncidentService incidentService;

    @GetMapping
    public ResponseEntity<Page<Incident>> listIncidents(
            @RequestParam UUID tenantId,
            @PageableDefault(size = 20, sort = "lastSeenAt") Pageable pageable) {
        return ResponseEntity.ok(incidentService.listIncidents(tenantId, pageable));
    }

    @GetMapping("/{incidentId}")
    public ResponseEntity<Incident> getIncident(@PathVariable UUID incidentId, @RequestParam UUID tenantId) {
        return ResponseEntity.ok(incidentService.getIncident(incidentId, tenantId));
    }

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

    @PatchMapping("/{incidentId}/acknowledge")
    public ResponseEntity<Void> acknowledgeIncident(
            @PathVariable UUID incidentId,
            @RequestParam UUID tenantId,
            @Valid @RequestBody ActorRequest request) {
        incidentService.acknowledgeIncident(incidentId, tenantId, request.getUserId());
        return ResponseEntity.noContent().build();
    }

    @PatchMapping("/{incidentId}/resolve")
    public ResponseEntity<Void> resolveIncident(
            @PathVariable UUID incidentId,
            @RequestParam UUID tenantId,
            @Valid @RequestBody ActorRequest request) {
        incidentService.resolveIncident(incidentId, tenantId, request.getUserId());
        return ResponseEntity.noContent().build();
    }
}
