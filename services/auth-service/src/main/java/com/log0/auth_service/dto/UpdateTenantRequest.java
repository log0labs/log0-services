package com.log0.auth_service.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.Getter;
import lombok.Setter;

/**
 * Request body for {@code PATCH /api/v1/tenants/me}: renames the caller's workspace.
 * The slug never changes because integrations may use it.
 */
@Getter
@Setter
public class UpdateTenantRequest {

    @NotBlank
    @Size(max = 100)
    private String name;
}
