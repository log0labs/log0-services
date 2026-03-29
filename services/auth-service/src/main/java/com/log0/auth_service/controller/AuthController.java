package com.log0.auth_service.controller;

import java.util.Map;
import java.util.UUID;

import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.log0.auth_service.dto.AuthResponse;
import com.log0.auth_service.dto.LoginRequest;
import com.log0.auth_service.dto.RefreshTokenRequest;
import com.log0.auth_service.security.AuthenticatedUser;
import com.log0.auth_service.service.ApiKeyService;
import com.log0.auth_service.service.AuthService;

import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;

/**
 * REST controller exposing the authentication API under {@code /api/v1/auth}.
 * Handles login, token refresh, and logout. All endpoints except logout are
 * public - they do not require a JWT to call.
 */
@RestController
@RequestMapping("/api/v1/auth")
@RequiredArgsConstructor
public class AuthController {

    private final AuthService authService;
    private final ApiKeyService apiKeyService;

    /**
     * Authenticates a user with email and password.
     * Returns a short-lived JWT access token and a long-lived refresh token.
     *
     * @param request the login credentials
     * @return {@code 200} with {@link AuthResponse} on success;
     *         {@code 401} if credentials are invalid
     */
    @PostMapping("/login")
    public ResponseEntity<AuthResponse> login(@Valid @RequestBody LoginRequest request) {
        return ResponseEntity.ok(authService.login(request));
    }

    /**
     * Issues a new access token and rotates the refresh token.
     * The old refresh token is immediately invalidated after this call.
     *
     * @param request the current refresh token
     * @return {@code 200} with a new {@link AuthResponse} on success;
     *         {@code 401} if the token is expired, revoked, or not found
     */
    @PostMapping("/refresh")
    public ResponseEntity<AuthResponse> refresh(@Valid @RequestBody RefreshTokenRequest request) {
        return ResponseEntity.ok(authService.refresh(request));
    }

    /**
     * Logs out the authenticated user by revoking all their active refresh tokens.
     * The current access token remains valid until it naturally expires (up to 1
     * hour).
     *
     * @param currentUser the authenticated principal extracted from the JWT by
     *                    {@link com.log0.auth_service.security.JwtAuthFilter}
     * @return {@code 204 No Content} on success
     */
    @PostMapping("/logout")
    public ResponseEntity<Void> logout(@AuthenticationPrincipal AuthenticatedUser currentUser) {
        authService.logout(currentUser.userId());
        return ResponseEntity.noContent().build();
    }

    /**
     * Validates an API key presented by the ingestion gateway.
     * The raw key is passed in the {@code X-Api-Key} header. On success,
     * returns the tenantId that owns the key so the gateway can attach it
     * to the log events it forwards downstream.
     *
     * <p>
     * This endpoint is public - it is called by a machine (the ingestion gateway),
     * not a human, and therefore cannot carry a JWT.
     *
     * @param apiKey the raw API key from the {@code X-Api-Key} header
     * @return {@code 200 OK} with {@code {"tenantId": "<uuid>"}} on success;
     *         {@code 404} if the key is invalid or revoked
     */
    @PostMapping("/validate-key")
    public ResponseEntity<Map<String, String>> validateApiKey(
            @RequestHeader("X-Api-Key") String apiKey) {
        UUID tenantId = apiKeyService.validateApiKey(apiKey);
        return ResponseEntity.ok(Map.of("tenantId", tenantId.toString()));
    }
}
