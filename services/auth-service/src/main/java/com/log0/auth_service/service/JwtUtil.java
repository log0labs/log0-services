package com.log0.auth_service.service;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Date;
import java.util.UUID;

import javax.crypto.SecretKey;

import org.springframework.stereotype.Component;

import com.log0.auth_service.config.JwtConfig;
import com.log0.auth_service.entity.User;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Utility for generating and validating JWT access tokens.
 * Tokens are signed with HMAC-SHA256 using the secret from {@link JwtConfig}.
 *
 * <p>
 * Claims included in every token:
 * <ul>
 * <li>{@code sub} - userId (standard JWT subject claim)</li>
 * <li>{@code tenantId} - UUID of the user's tenant</li>
 * <li>{@code role} - the user's role (ADMIN, ENGINEER, VIEWER)</li>
 * <li>{@code iat} - issued-at timestamp</li>
 * <li>{@code exp} - expiry timestamp</li>
 * </ul>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class JwtUtil {

    private final JwtConfig jwtConfig;
    private SecretKey signingKey;

    /**
     * Builds the {@link SecretKey} from the raw secret string after Spring
     * has injected {@link JwtConfig}. Called automatically by Spring before
     * the bean is used for the first time.
     */
    @PostConstruct
    private void init() {
        this.signingKey = Keys.hmacShaKeyFor(
                jwtConfig.getSecret().getBytes(StandardCharsets.UTF_8));
    }

    /**
     * Generates a signed JWT access token for the given user.
     * The token expires after the duration configured in
     * {@code jwt.access-token-expiry-seconds}.
     *
     * @param user the authenticated user to issue a token for
     * @return a compact, URL-safe JWT string (header.payload.signature)
     */
    public String generateAccessToken(User user) {
        Instant now = Instant.now();
        Instant expiry = now.plusSeconds(jwtConfig.getAccessTokenExpirySeconds());

        return Jwts.builder()
                .subject(user.getUserId().toString())
                .claim("tenantId", user.getTenant().getTenantId().toString())
                .claim("role", user.getRole().name())
                .issuedAt(Date.from(now))
                .expiration(Date.from(expiry))
                .signWith(signingKey)
                .compact();
    }

    /**
     * Parses and validates a JWT access token, returning its claims if valid.
     * Throws {@link JwtException} if the token is malformed, expired, or the
     * signature does not match.
     *
     * @param token the compact JWT string to validate
     * @return the verified claims extracted from the token
     * @throws JwtException if the token is invalid for any reason
     */
    public Claims parseToken(String token) {
        return Jwts.parser()
                .verifyWith(signingKey)
                .build()
                .parseSignedClaims(token)
                .getPayload();
    }

    /**
     * Extracts the userId ({@code sub} claim) from a validated token.
     *
     * @param claims the claims returned by {@link #parseToken(String)}
     * @return the userId as a {@link UUID}
     */
    public UUID extractUserId(Claims claims) {
        return UUID.fromString(claims.getSubject());
    }

    /**
     * Extracts the tenantId custom claim from a validated token.
     *
     * @param claims the claims returned by {@link #parseToken(String)}
     * @return the tenantId as a {@link UUID}
     */
    public UUID extractTenantId(Claims claims) {
        return UUID.fromString(claims.get("tenantId", String.class));
    }

    /**
     * Extracts the role custom claim from a validated token.
     *
     * @param claims the claims returned by {@link #parseToken(String)}
     * @return the role as a {@link User.Role} enum value
     */
    public User.Role extractRole(Claims claims) {
        return User.Role.valueOf(claims.get("role", String.class));
    }
}
