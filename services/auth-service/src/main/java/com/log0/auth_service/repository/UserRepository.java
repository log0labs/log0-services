package com.log0.auth_service.repository;

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
}
