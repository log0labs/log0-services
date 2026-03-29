package com.log0.auth_service.service;

import java.util.List;
import java.util.UUID;

import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.log0.auth_service.dto.CreateUserRequest;
import com.log0.auth_service.dto.UserResponse;
import com.log0.auth_service.entity.Tenant;
import com.log0.auth_service.entity.User;
import com.log0.auth_service.exception.DuplicateResourceException;
import com.log0.auth_service.exception.ResourceNotFoundException;
import com.log0.auth_service.repository.TenantRepository;
import com.log0.auth_service.repository.UserRepository;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Handles user management within a tenant - creating new users and listing
 * existing ones. All operations are tenant-scoped: a user can only be created
 * under their own tenant, and listing returns only users within that tenant.
 *
 * <p>
 * These endpoints are protected and require the caller to have the
 * {@code ADMIN} role, enforced at the controller layer via Spring Security.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class UserService {

    private final UserRepository userRepository;
    private final TenantRepository tenantRepository;
    private final PasswordEncoder passwordEncoder;

    /**
     * Creates a new user under the given tenant.
     * The caller's tenantId (extracted from their JWT) is used to scope the
     * new user - it is not taken from the request body.
     *
     * @param tenantId the tenant to create the user under, from the caller's JWT
     * @param request  the new user's email, password, and role
     * @return a {@link UserResponse} representing the newly created user
     * @throws ResourceNotFoundException  if the tenant does not exist
     * @throws DuplicateResourceException if the email is already registered
     */
    @Transactional
    public UserResponse createUser(UUID tenantId, CreateUserRequest request) {
        Tenant tenant = tenantRepository.findById(tenantId)
                .orElseThrow(() -> new ResourceNotFoundException("Tenant not found: " + tenantId));

        if (userRepository.existsByEmail(request.getEmail())) {
            throw new DuplicateResourceException("Email already registered: " + request.getEmail());
        }

        User user = new User();
        user.setTenant(tenant);
        user.setEmail(request.getEmail());
        user.setPasswordHash(passwordEncoder.encode(request.getPassword()));
        user.setRole(request.getRole());
        userRepository.save(user);

        log.info("Created user {} under tenant {}", user.getUserId(), tenantId);

        return UserResponse.from(user);
    }

    /**
     * Returns all users belonging to the given tenant.
     * Each user is mapped to a {@link UserResponse} - password hashes are
     * never included in the response.
     *
     * @param tenantId the tenant whose users to list, from the caller's JWT
     * @return list of users in the tenant, may be empty but never null
     */
    public List<UserResponse> listUsers(UUID tenantId) {
        return userRepository.findAll().stream()
                .filter(u -> u.getTenant().getTenantId().equals(tenantId))
                .map(UserResponse::from)
                .toList();
    }
}
