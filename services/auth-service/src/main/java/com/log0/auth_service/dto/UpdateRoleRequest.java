package com.log0.auth_service.dto;

import com.log0.auth_service.entity.User.Role;

import jakarta.validation.constraints.NotNull;
import lombok.Getter;
import lombok.Setter;

/**
 * Request body for {@code PATCH /api/v1/users/{userId}/role}.
 */
@Getter
@Setter
public class UpdateRoleRequest {

    /** The new role: {@code ADMIN}, {@code ENGINEER} or {@code VIEWER}. */
    @NotNull
    private Role role;
}
