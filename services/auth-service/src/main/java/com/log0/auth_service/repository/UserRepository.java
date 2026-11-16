package com.log0.auth_service.repository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;

import com.log0.auth_service.entity.User;

/**
 * Spring Data JPA repository for {@link User}.
 * Provides user lookups by email (login) and by tenant (admin user management).
 */
public interface UserRepository extends JpaRepository<User, UUID> {

    /**
     * Finds a user by their email address.
     * Used during login to load the user record before verifying the password.
     *
     * @param email the email address to look up
     * @return the matching user, or empty if no account exists for that email
     */
    Optional<User> findByEmail(String email);

    /**
     * Checks whether an email address is already registered.
     * Used during registration to reject duplicate accounts before attempting
     * an insert.
     *
     * @param email the email address to check
     * @return {@code true} if an account with this email already exists
     */
    boolean existsByEmail(String email);

    /**
     * Lists the users of one tenant.
     *
     * @param tenantId the tenant whose users to list
     * @return the tenant's users, possibly empty
     */
    List<User> findByTenantTenantId(UUID tenantId);

    /**
     * Finds a user only if they belong to the given tenant, so one tenant can never
     * reach another tenant's users by guessing an id.
     *
     * @param userId   the user to find
     * @param tenantId the tenant the user must belong to
     * @return the user, or empty if there is none in that tenant
     */
    Optional<User> findByUserIdAndTenantTenantId(UUID userId, UUID tenantId);

    /**
     * Counts the users of a tenant that have a given role. Used to keep at least one admin.
     *
     * @param tenantId the tenant to count in
     * @param role     the role to count
     * @return how many users have that role
     */
    long countByTenantTenantIdAndRole(UUID tenantId, User.Role role);
}
