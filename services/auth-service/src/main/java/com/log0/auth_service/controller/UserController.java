package com.log0.auth_service.controller;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.log0.auth_service.dto.CreateUserRequest;
import com.log0.auth_service.dto.UpdateRoleRequest;
import com.log0.auth_service.dto.UserResponse;
import com.log0.auth_service.security.AuthenticatedUser;
import com.log0.auth_service.service.UserService;

import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;

/**
 * REST controller exposing user management under {@code /api/v1/users}.
 * All endpoints require the {@code ADMIN} role, enforced by
 * {@link com.log0.auth_service.security.SecurityConfig}.
 * The acting user's tenantId is always extracted from their JWT - users
 * can only manage other users within their own tenant.
 */
@RestController
@RequestMapping("/api/v1/users")
@RequiredArgsConstructor
public class UserController {

    private final UserService userService;

    /**
     * Creates a new user under the authenticated admin's tenant.
     *
     * @param request     the new user's email, password, and role
     * @param currentUser the authenticated admin, injected from the JWT
     * @return {@code 201 Created} with the {@link UserResponse} on success;
     *         {@code 409 Conflict} if the email is already registered;
     *         {@code 400 Bad Request} if validation fails
     */
    @PostMapping
    public ResponseEntity<UserResponse> createUser(
            @Valid @RequestBody CreateUserRequest request,
            @AuthenticationPrincipal AuthenticatedUser currentUser) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(userService.createUser(currentUser.tenantId(), request));
    }

    /**
     * Lists all users in the authenticated admin's tenant.
     *
     * @param currentUser the authenticated admin, injected from the JWT
     * @return {@code 200 OK} with a list of {@link UserResponse} objects;
     *         may be empty if only the admin exists
     */
    @GetMapping
    public ResponseEntity<List<UserResponse>> listUsers(
            @AuthenticationPrincipal AuthenticatedUser currentUser) {
        return ResponseEntity.ok(userService.listUsers(currentUser.tenantId()));
    }

    /**
     * Changes a user's role within the admin's tenant.
     *
     * @param userId      the user to change
     * @param request     the new role
     * @param currentUser the authenticated admin, injected from the JWT
     * @return {@code 200 OK} with the updated user; {@code 404} if the user is not in the
     *         tenant; {@code 409} if it is your own role or the last admin
     */
    @PatchMapping("/{userId}/role")
    public ResponseEntity<UserResponse> updateRole(
            @PathVariable UUID userId,
            @Valid @RequestBody UpdateRoleRequest request,
            @AuthenticationPrincipal AuthenticatedUser currentUser) {
        return ResponseEntity.ok(userService.updateRole(
                currentUser.tenantId(), currentUser.userId(), userId, request.getRole()));
    }

    /**
     * Removes a user from the admin's tenant.
     *
     * @param userId      the user to remove
     * @param currentUser the authenticated admin, injected from the JWT
     * @return {@code 204 No Content}; {@code 404} if the user is not in the tenant;
     *         {@code 409} if it is yourself or the last admin
     */
    @DeleteMapping("/{userId}")
    public ResponseEntity<Void> deleteUser(
            @PathVariable UUID userId,
            @AuthenticationPrincipal AuthenticatedUser currentUser) {
        userService.deleteUser(currentUser.tenantId(), currentUser.userId(), userId);
        return ResponseEntity.noContent().build();
    }
}
