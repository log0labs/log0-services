package com.log0.auth_service.dto;

import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import lombok.Getter;
import lombok.Setter;

/**
 * Request body for {@code POST /api/v1/tenants/register}.
 * Creates a new tenant and its first ADMIN user in a single operation.
 * The slug must be globally unique — registration fails if it is already taken.
 */
@Getter
@Setter
public class RegisterTenantRequest {

    /** Human-readable organisation name (e.g. "Acme Corp"). */
    @NotBlank
    private String tenantName;

    /**
     * URL-safe unique identifier for the tenant (e.g. {@code acme-corp}).
     * Only lowercase letters, digits, and hyphens are allowed.
     */
    @NotBlank
    @Pattern(regexp = "^[a-z0-9-]+$", message = "slug must contain only lowercase letters, digits, and hyphens")
    private String slug;

    /** Email address for the first admin user of this tenant. */
    @NotBlank
    @Email
    private String adminEmail;

    /** Password for the first admin user. Minimum 8 characters. */
    @NotBlank
    @Size(min = 8, message = "password must be at least 8 characters")
    private String adminPassword;
}
