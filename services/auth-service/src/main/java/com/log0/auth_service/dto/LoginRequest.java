package com.log0.auth_service.dto;

import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import lombok.Getter;
import lombok.Setter;

/**
 * Request body for {@code POST /api/v1/auth/login}.
 * Credentials are validated against the {@code app_user} table.
 * On success, an access token and refresh token are returned.
 */
@Getter
@Setter
public class LoginRequest {

    @NotBlank
    @Email
    private String email;

    @NotBlank
    private String password;
}
