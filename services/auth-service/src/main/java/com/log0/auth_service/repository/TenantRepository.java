package com.log0.auth_service.repository;

import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;

import com.log0.auth_service.entity.Tenant;

/**
 * Spring Data JPA repository for {@link Tenant}.
 * Provides tenant lookup by slug, used during registration to enforce
 * uniqueness and during login to resolve the tenant from the request.
 */
public interface TenantRepository extends JpaRepository<Tenant, UUID> {

    /**
     * Finds a tenant by its unique URL-safe slug.
     * Used at registration time to check whether the slug is already taken.
     *
     * @param slug the slug to look up (e.g. {@code acme-corp})
     * @return the matching tenant, or empty if no tenant has that slug
     */
    Optional<Tenant> findBySlug(String slug);
}
