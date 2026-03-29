package com.log0.auth_service.security;

import java.util.UUID;

import com.log0.auth_service.entity.User.Role;

/**
 * Represents the authenticated principal attached to every secured request.
 * Populated by {@link JwtAuthFilter} from the validated JWT claims and stored
 * in Spring Security's {@code SecurityContext}.
 *
 * <p>
 * Controllers access this via
 * {@code @AuthenticationPrincipal AuthenticatedUser currentUser},
 * which gives them the userId and tenantId without an extra database lookup.
 *
 * @param userId   the authenticated user's ID (from the JWT {@code sub} claim)
 * @param tenantId the tenant the user belongs to (from the JWT {@code tenantId}
 *                 claim)
 * @param role     the user's role (from the JWT {@code role} claim)
 */
public record AuthenticatedUser(UUID userId, UUID tenantId, Role role) {
}
