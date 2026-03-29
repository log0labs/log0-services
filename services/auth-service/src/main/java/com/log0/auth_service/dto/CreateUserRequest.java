package com.log0.auth_service.dto;

import com.log0.auth_service.entity.User.Role;

import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import lombok.Getter;
import lombok.Setter;

/**
 * Request body for {@code POST /api/v1/users}.
 * Used by a tenant ADMIN to add a new user to their organisation.
 * The new user is automatically assigned to the same tenant as the requester.
 */
@Getter
@Setter
public class CreateUserRequest {

    @NotBlank
    @Email
    private String email;

    @NotBlank
    @Size(min = 8, message = "password must be at least 8 characters")
    private String password;

    /**
     * Role to assign to the new user.
     * Must be one of: {@code ADMIN}, {@code ENGINEER}, {@code VIEWER}.
     */
    @NotNull
    private Role role;
}
