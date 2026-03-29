package com.log0.auth_service.dto;

import lombok.Builder;
import lombok.Getter;

/**
 * Response body for login ({@code POST /api/v1/auth/login}) and token refresh
 * ({@code POST /api/v1/auth/refresh}).
 * The client stores both tokens: the access token is sent on every API request,
 * the refresh token is used only to obtain a new access token when the current
 * one expires.
 */
@Getter
@Builder
public class AuthResponse {

    /** Short-lived JWT access token (1 hour). Sent as Bearer token on API calls. */
    private String accessToken;

    /**
     * Long-lived refresh token (30 days).
     * Store securely — presenting this token issues a new access token.
     * Each use rotates this token; the previous value is immediately invalidated.
     */
    private String refreshToken;
}
