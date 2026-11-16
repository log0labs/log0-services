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
import com.log0.auth_service.exception.OperationNotAllowedException;
import com.log0.auth_service.exception.ResourceNotFoundException;
import com.log0.auth_service.entity.User.Role;
import com.log0.auth_service.repository.RefreshTokenRepository;
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
    private final RefreshTokenRepository refreshTokenRepository;
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
        return userRepository.findByTenantTenantId(tenantId).stream()
                .map(UserResponse::from)
                .toList();
    }

    /**
     * Changes a user's role. Two rules keep a workspace from locking itself out: you cannot
     * change your own role, and the last admin cannot be demoted.
     *
     * @param tenantId the caller's tenant, from the JWT
     * @param actorId  the calling admin, from the JWT
     * @param userId   the user to change
     * @param role     the new role
     * @return the updated user
     * @throws ResourceNotFoundException    if the user is not in the caller's tenant
     * @throws OperationNotAllowedException if the change breaks one of the rules above
     */
    @Transactional
    public UserResponse updateRole(UUID tenantId, UUID actorId, UUID userId, Role role) {
        User user = userRepository.findByUserIdAndTenantTenantId(userId, tenantId)
                .orElseThrow(() -> new ResourceNotFoundException("User not found: " + userId));
        if (user.getUserId().equals(actorId)) {
            throw new OperationNotAllowedException("You cannot change your own role");
        }
        if (user.getRole() == Role.ADMIN && role != Role.ADMIN
                && userRepository.countByTenantTenantIdAndRole(tenantId, Role.ADMIN) <= 1) {
            throw new OperationNotAllowedException("The workspace needs at least one admin");
        }
        user.setRole(role);
        userRepository.save(user);
        // the role is read from the access token, so open sessions must sign in again to see it
        refreshTokenRepository.revokeAllByUserId(user.getUserId());
        log.info("User {} role set to {} in tenant {}", userId, role, tenantId);
        return UserResponse.from(user);
    }

    /**
     * Removes a user and their sessions. You cannot delete yourself, and the last admin
     * cannot be deleted.
     *
     * @param tenantId the caller's tenant, from the JWT
     * @param actorId  the calling admin, from the JWT
     * @param userId   the user to remove
     * @throws ResourceNotFoundException    if the user is not in the caller's tenant
     * @throws OperationNotAllowedException if the removal breaks one of the rules above
     */
    @Transactional
    public void deleteUser(UUID tenantId, UUID actorId, UUID userId) {
        User user = userRepository.findByUserIdAndTenantTenantId(userId, tenantId)
                .orElseThrow(() -> new ResourceNotFoundException("User not found: " + userId));
        if (user.getUserId().equals(actorId)) {
            throw new OperationNotAllowedException("You cannot delete yourself");
        }
        if (user.getRole() == Role.ADMIN
                && userRepository.countByTenantTenantIdAndRole(tenantId, Role.ADMIN) <= 1) {
            throw new OperationNotAllowedException("The workspace needs at least one admin");
        }
        refreshTokenRepository.deleteAllByUserId(user.getUserId());
        userRepository.delete(user);
        log.info("Deleted user {} from tenant {}", userId, tenantId);
    }
}
