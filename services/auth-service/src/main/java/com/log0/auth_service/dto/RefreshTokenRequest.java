package com.log0.auth_service.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.Getter;
import lombok.Setter;

/**
 * Request body for {@code POST /api/v1/auth/refresh}.
 * The client sends the refresh token it received at login.
 * On success, a new access token and rotated refresh token are returned.
 */
@Getter
@Setter
public class RefreshTokenRequest {

    /** The raw refresh token string issued at login or last refresh. */
    @NotBlank
    private String refreshToken;
}
