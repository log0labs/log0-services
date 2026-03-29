package com.log0.auth_service.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.Getter;
import lombok.Setter;

/**
 * Request body for {@code POST /api/v1/api-keys}.
 * Used by a tenant ADMIN to generate a new API key for their organisation.
 * The actual key value is generated server-side and returned once in the
 * response.
 */
@Getter
@Setter
public class CreateApiKeyRequest {

    /** Human-readable label for this key (e.g. "Production Ingestor"). */
    @NotBlank
    private String name;
}
