package com.log0.auth_service.dto;

import java.util.UUID;

import com.log0.auth_service.entity.Tenant;

import lombok.Builder;
import lombok.Getter;

/**
 * Response body representing a tenant.
 * Returned by tenant registration and tenant info endpoints.
 * Raw JPA entities are never returned directly from controllers -
 * this DTO controls exactly which fields are exposed in the API.
 */
@Getter
@Builder
public class TenantResponse {

    private UUID tenantId;
    private String name;
    private String slug;

    /**
     * Convenience factory method to build a {@link TenantResponse} from a
     * {@link Tenant} entity, keeping the mapping in one place.
     *
     * @param tenant the entity to convert
     * @return a response DTO populated from the entity
     */
    public static TenantResponse from(Tenant tenant) {
        return TenantResponse.builder()
                .tenantId(tenant.getTenantId())
                .name(tenant.getName())
                .slug(tenant.getSlug())
                .build();
    }
}
