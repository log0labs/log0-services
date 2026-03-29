package com.log0.auth_service.service;

import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.log0.auth_service.dto.RegisterTenantRequest;
import com.log0.auth_service.dto.TenantResponse;
import com.log0.auth_service.entity.Tenant;
import com.log0.auth_service.entity.User;
import com.log0.auth_service.entity.User.Role;
import com.log0.auth_service.exception.DuplicateResourceException;
import com.log0.auth_service.repository.TenantRepository;
import com.log0.auth_service.repository.UserRepository;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Handles tenant registration — the entry point for a new organisation
 * joining the platform. Registration creates both the {@link Tenant} record
 * and the first {@link User} with role {@code ADMIN} in a single transaction,
 * so either both are created or neither is.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class TenantService {

    private final TenantRepository tenantRepository;
    private final UserRepository userRepository;
    private final PasswordEncoder passwordEncoder;

    /**
     * Registers a new tenant and its first ADMIN user.
     * Validates that neither the slug nor the admin email is already taken
     * before persisting anything.
     *
     * @param request the registration payload containing tenant name, slug,
     *                admin email, and admin password
     * @return a {@link TenantResponse} representing the newly created tenant
     * @throws DuplicateResourceException if the slug or admin email is already in
     *                                    use
     */
    @Transactional
    public TenantResponse register(RegisterTenantRequest request) {
        if (tenantRepository.findBySlug(request.getSlug()).isPresent()) {
            throw new DuplicateResourceException("Slug already taken: " + request.getSlug());
        }

        if (userRepository.existsByEmail(request.getAdminEmail())) {
            throw new DuplicateResourceException("Email already registered: " + request.getAdminEmail());
        }

        Tenant tenant = new Tenant();
        tenant.setName(request.getTenantName());
        tenant.setSlug(request.getSlug());
        tenantRepository.save(tenant);

        User admin = new User();
        admin.setTenant(tenant);
        admin.setEmail(request.getAdminEmail());
        admin.setPasswordHash(passwordEncoder.encode(request.getAdminPassword()));
        admin.setRole(Role.ADMIN);
        userRepository.save(admin);

        log.info("Registered new tenant: {} (slug: {})", tenant.getTenantId(), tenant.getSlug());

        return TenantResponse.from(tenant);
    }
}
