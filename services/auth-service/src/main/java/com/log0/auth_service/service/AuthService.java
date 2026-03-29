package com.log0.auth_service.service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.HexFormat;
import java.util.UUID;

import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.log0.auth_service.config.JwtConfig;
import com.log0.auth_service.dto.AuthResponse;
import com.log0.auth_service.dto.LoginRequest;
import com.log0.auth_service.dto.RefreshTokenRequest;
import com.log0.auth_service.entity.RefreshToken;
import com.log0.auth_service.entity.User;
import com.log0.auth_service.exception.InvalidCredentialsException;
import com.log0.auth_service.exception.InvalidTokenException;
import com.log0.auth_service.repository.RefreshTokenRepository;
import com.log0.auth_service.repository.UserRepository;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Handles the authentication lifecycle: login, token refresh, and logout.
 *
 * <p>
 * Login verifies credentials and issues both an access token (short-lived JWT)
 * and a refresh token (long-lived, stored as a SHA-256 hash in the database).
 *
 * <p>
 * Token refresh validates the presented refresh token, rotates it (revokes the
 * old one and issues a new one), and returns a fresh access token. Rotation
 * means a stolen refresh token can only be used once before it is invalidated.
 *
 * <p>
 * Logout revokes all active refresh tokens for the user, invalidating every
 * open session across all devices.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class AuthService {

    private final UserRepository userRepository;
    private final RefreshTokenRepository refreshTokenRepository;
    private final PasswordEncoder passwordEncoder;
    private final JwtUtil jwtUtil;
    private final JwtConfig jwtConfig;

    /**
     * Authenticates a user with their email and password.
     * On success, issues a JWT access token and a refresh token.
     * On failure (wrong email or wrong password), throws
     * {@link InvalidCredentialsException} with the same generic message
     * to prevent user enumeration.
     *
     * @param request the login payload containing email and password
     * @return an {@link AuthResponse} with both tokens
     * @throws InvalidCredentialsException if the email is not found or the
     *                                     password does not match
     */
    @Transactional
    public AuthResponse login(LoginRequest request) {
        User user = userRepository.findByEmail(request.getEmail())
                .orElseThrow(InvalidCredentialsException::new);

        if (!passwordEncoder.matches(request.getPassword(), user.getPasswordHash())) {
            throw new InvalidCredentialsException();
        }

        String accessToken = jwtUtil.generateAccessToken(user);
        String rawRefreshToken = issueRefreshToken(user);

        log.info("User {} logged in", user.getUserId());

        return AuthResponse.builder()
                .accessToken(accessToken)
                .refreshToken(rawRefreshToken)
                .build();
    }

    /**
     * Rotates a refresh token and issues a new access token.
     * The presented token is looked up by its hash, validated (not revoked,
     * not expired), then immediately marked as revoked. A new refresh token
     * is issued in its place.
     *
     * @param request the refresh payload containing the raw refresh token
     * @return an {@link AuthResponse} with a new access token and new refresh token
     * @throws InvalidTokenException if the token is not found, already revoked,
     *                               or past its expiry date
     */
    @Transactional
    public AuthResponse refresh(RefreshTokenRequest request) {
        String tokenHash = sha256(request.getRefreshToken());

        RefreshToken refreshToken = refreshTokenRepository.findByTokenHash(tokenHash)
                .orElseThrow(() -> new InvalidTokenException("Refresh token not found"));

        if (refreshToken.isRevoked()) {
            throw new InvalidTokenException("Refresh token has been revoked");
        }

        if (Instant.now().isAfter(refreshToken.getExpiresAt())) {
            throw new InvalidTokenException("Refresh token has expired");
        }

        refreshToken.setRevoked(true);
        refreshTokenRepository.save(refreshToken);

        User user = refreshToken.getUser();
        String accessToken = jwtUtil.generateAccessToken(user);
        String rawRefreshToken = issueRefreshToken(user);

        log.info("Refreshed tokens for user {}", user.getUserId());

        return AuthResponse.builder()
                .accessToken(accessToken)
                .refreshToken(rawRefreshToken)
                .build();
    }

    /**
     * Logs out a user by revoking all their active refresh tokens.
     * The access token remains valid until it naturally expires (up to 1 hour),
     * as JWTs are stateless and cannot be revoked. The short TTL mitigates this.
     *
     * @param userId the ID of the user to log out, extracted from their JWT
     *               by the controller
     */
    @Transactional
    public void logout(UUID userId) {
        refreshTokenRepository.revokeAllByUserId(userId);
        log.info("Logged out user {}, all refresh tokens revoked", userId);
    }

    /**
     * Generates a new raw refresh token, stores its SHA-256 hash in the
     * database, and returns the raw value to give to the client.
     *
     * @param user the user to issue the token for
     * @return the raw refresh token string (shown to the client once)
     */
    private String issueRefreshToken(User user) {
        String rawToken = generateRawToken();
        String tokenHash = sha256(rawToken);

        RefreshToken refreshToken = new RefreshToken();
        refreshToken.setUser(user);
        refreshToken.setTokenHash(tokenHash);
        refreshToken.setExpiresAt(Instant.now().plus(jwtConfig.getRefreshTokenExpiryDays(), ChronoUnit.DAYS));
        refreshTokenRepository.save(refreshToken);

        return rawToken;
    }

    /**
     * Generates a cryptographically random 32-byte token encoded as a
     * 64-character hex string.
     *
     * @return a unique, unpredictable token string
     */
    private String generateRawToken() {
        byte[] bytes = new byte[32];
        new SecureRandom().nextBytes(bytes);
        return HexFormat.of().formatHex(bytes);
    }

    /**
     * Computes the SHA-256 hash of the given string and returns it as a
     * 64-character lowercase hex string.
     *
     * @param input the string to hash
     * @return the hex-encoded SHA-256 digest
     * @throws IllegalStateException if SHA-256 is unavailable (guaranteed on all
     *                               JVMs)
     */
    private String sha256(String input) {
        try {
            byte[] hash = MessageDigest.getInstance("SHA-256")
                    .digest(input.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(hash);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 not available", e);
        }
    }
}
