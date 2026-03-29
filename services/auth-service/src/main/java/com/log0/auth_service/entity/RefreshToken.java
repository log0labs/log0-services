package com.log0.auth_service.entity;

import java.time.Instant;
import java.util.UUID;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
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
 * JPA entity representing a refresh token in the {@code refresh_token} table.
 * Refresh tokens allow users to obtain new access tokens after the 1-hour
 * access token expires, without re-entering their password.
 *
 * <p>
 * Only the SHA-256 hash of the raw token is stored. On each use, the token is
 * rotated: the current record is marked {@code revoked = true} and a new
 * refresh token is issued. This means a stolen token can only be used once
 * before it is invalidated.
 */
@Entity
@Table(name = "refresh_token")
@Getter
@Setter
public class RefreshToken {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @Column(name = "token_id")
    private UUID tokenId;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "user_id", nullable = false)
    private User user;

    /**
     * SHA-256 hash of the raw refresh token string.
     * The raw token is returned to the client once and never stored.
     */
    @Column(name = "token_hash", nullable = false, unique = true)
    private String tokenHash;

    /** When this token was issued. */
    @Column(name = "issued_at", nullable = false, updatable = false)
    private Instant issuedAt;

    /** When this token expires. Checked on every refresh request. */
    @Column(name = "expires_at", nullable = false)
    private Instant expiresAt;

    /**
     * Whether this token has been consumed or invalidated.
     * A revoked token is rejected immediately even if it has not yet expired.
     * Tokens are revoked on use (rotation) and on logout.
     */
    @Column(name = "revoked", nullable = false)
    private boolean revoked = false;

    @PrePersist
    void onCreate() {
        issuedAt = Instant.now();
    }
}