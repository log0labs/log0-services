package com.log0.incident_service.statemachine;

import java.util.Map;
import java.util.Set;

import org.springframework.stereotype.Component;

/**
 * Enforces the allowed incident status transitions as a simple allow-list:
 * {@code NEW → ASSIGNED → ACKNOWLEDGED → RESOLVED}. Re-assignment from
 * {@code ASSIGNED} to {@code ASSIGNED} is permitted to support handovers.
 * {@code RESOLVED} is a terminal state with no outgoing transitions.
 */
@Component
public class IncidentStateMachine {
    private static final Map<String, Set<String>> ALLOWED_TRANSITIONS = Map.of(
        "NEW", Set.of("ASSIGNED"),
        "ASSIGNED", Set.of("ACKNOWLEDGED", "ASSIGNED"),
        "ACKNOWLEDGED", Set.of("RESOLVED"),
        "RESOLVED", Set.of()
    );

    /**
     * Validates that moving from {@code currentStatus} to {@code targetStatus} is permitted.
     *
     * @param currentStatus the incident's present status
     * @param targetStatus  the desired next status
     * @throws IllegalStateException if the transition is not allowed
     */
    public void transition(String currentStatus, String targetStatus) {
        Set<String> allowed = ALLOWED_TRANSITIONS.getOrDefault(currentStatus, Set.of());
        if (!allowed.contains(targetStatus)) {
            throw new IllegalStateException(
                "Invalid transition: " + currentStatus + " → " + targetStatus
            );
        }
    }
}
