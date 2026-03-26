package com.log0.incident_service.statemachine;

import java.util.Map;
import java.util.Set;

import org.springframework.stereotype.Component;

@Component
public class IncidentStateMachine {
    private static final Map<String, Set<String>> ALLOWED_TRANSITIONS = Map.of(
        "NEW", Set.of("ASSIGNED"),
        "ASSIGNED", Set.of("ACKNOWLEDGED", "ASSIGNED"),
        "ACKNOWLEDGED", Set.of("RESOLVED"),
        "RESOLVED", Set.of()
    );
    
    public void transition(String currentStatus, String targetStatus) {
        Set<String> allowed = ALLOWED_TRANSITIONS.getOrDefault(currentStatus, Set.of());
        if (!allowed.contains(targetStatus)) {
            throw new IllegalStateException(
                "Invalid transition: " + currentStatus + " → " + targetStatus
            );
        }
    }
}
