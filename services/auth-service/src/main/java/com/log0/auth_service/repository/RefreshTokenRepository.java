package com.log0.auth_service.repository;

import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;

import com.log0.auth_service.entity.RefreshToken;

/**
 * Spring Data JPA repository for {@link RefreshToken}.
 * Provides token lookup by hash (refresh path) and bulk revocation
 * by user (logout-all-devices path).
 */
public interface RefreshTokenRepository extends JpaRepository<RefreshToken, UUID> {

    /**
     * Finds a refresh token by its SHA-256 hash.
     * Called during the token refresh flow to locate the record before
     * checking expiry and revocation status.
     *
     * @param tokenHash the SHA-256 hash of the raw refresh token string
     * @return the matching token record, or empty if not found
     */
    Optional<RefreshToken> findByTokenHash(String tokenHash);

    /**
     * Revokes all active refresh tokens for a given user.
     * Called on logout to invalidate every session the user has open,
     * across all devices.
     *
     * @param userId the user whose tokens should be revoked
     */
    @Modifying
    @Query("UPDATE RefreshToken rt SET rt.revoked = true WHERE rt.user.userId = :userId AND rt.revoked = false")
    void revokeAllByUserId(UUID userId);
}
