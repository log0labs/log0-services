package com.log0.auth_service.entity;

import java.time.Instant;
import java.util.UUID;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.FetchType;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.PrePersist;
import jakarta.persistence.Table;
import lombok.Getter;
import lombok.Setter;

/**
 * JPA entity representing a human user in the {@code app_user} table.
 * Each user belongs to exactly one tenant and has a single role that controls
 * what they can see and do in the frontend.
 *
 * <p>
 * Passwords are stored as BCrypt hashes — the plaintext password is never
 * persisted. Authentication is handled by {@code AuthService}, which compares
 * the provided password against {@code passwordHash} using BCrypt.
 */
@Entity
@Table(name = "app_user")
@Getter
@Setter
public class User {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @Column(name = "user_id")
    private UUID userId;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "tenant_id", nullable = false)
    private Tenant tenant;

    /** Email address used as the login identifier. Unique within the system. */
    @Column(name = "email", nullable = false, unique = true)
    private String email;

    /**
     * BCrypt hash of the user's password.
     * Never store or log the raw password.
     */
    @Column(name = "password_hash", nullable = false)
    private String passwordHash;

    /**
     * Role that determines the user's permissions.
     * Stored in the DB as the enum name string (e.g. {@code "ADMIN"}).
     */
    @Enumerated(EnumType.STRING)
    @Column(name = "role", nullable = false)
    private Role role;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @PrePersist
    void onCreate() {
        createdAt = Instant.now();
    }

    /** Roles that can be assigned to a user within a tenant. */
    public enum Role {
        /** Full access: manage users, API keys, and all incidents. */
        ADMIN,
        /** Can view and work incidents but cannot manage users or API keys. */
        ENGINEER,
        /** Read-only access to incidents and dashboards. */
        VIEWER
    }
}
