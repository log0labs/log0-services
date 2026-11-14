package com.log0.incident_service.repository;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import org.springframework.data.jpa.domain.Specification;

import com.log0.incident_service.entity.Incident;

import jakarta.persistence.criteria.Predicate;

/**
 * Builds the optional filters of the incident list. Every filter is optional except the tenant,
 * which is always applied so one tenant can never list another's incidents.
 */
public final class IncidentSpecs {

    private IncidentSpecs() {
    }

    /**
     * @param tenantId    the owning tenant (always applied)
     * @param status      exact status, or null for any
     * @param severity    exact severity, or null for any
     * @param serviceName part of the service name, case-insensitive, or null for any
     * @param environment exact environment, or null for any
     * @return the combined specification
     */
    public static Specification<Incident> filter(
            UUID tenantId, String status, String severity, String serviceName, String environment) {
        return (root, query, cb) -> {
            List<Predicate> all = new ArrayList<>();
            all.add(cb.equal(root.get("tenantId"), tenantId));
            if (hasText(status)) {
                all.add(cb.equal(root.get("status"), status.trim().toUpperCase()));
            }
            if (hasText(severity)) {
                all.add(cb.equal(root.get("severity"), severity.trim().toUpperCase()));
            }
            if (hasText(environment)) {
                all.add(cb.equal(root.get("environment"), environment.trim()));
            }
            if (hasText(serviceName)) {
                all.add(cb.like(
                        cb.lower(root.get("serviceName")),
                        "%" + escapeLike(serviceName.trim().toLowerCase()) + "%",
                        '\\'));
            }
            return cb.and(all.toArray(new Predicate[0]));
        };
    }

    private static boolean hasText(String value) {
        return value != null && !value.isBlank();
    }

    /** Makes {@code %} and {@code _} in user input match themselves instead of acting as wildcards. */
    public static String escapeLike(String value) {
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_");
    }
}
