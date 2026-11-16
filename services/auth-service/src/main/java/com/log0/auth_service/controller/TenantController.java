package com.log0.auth_service.controller;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.log0.auth_service.dto.RegisterTenantRequest;
import com.log0.auth_service.dto.TenantResponse;
import com.log0.auth_service.dto.UpdateTenantRequest;
import com.log0.auth_service.security.AuthenticatedUser;
import com.log0.auth_service.service.TenantService;

import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;

/**
 * REST controller exposing tenant management under {@code /api/v1/tenants}.
 * Currently handles only tenant registration, which is a public endpoint -
 * no JWT is required to create a new tenant.
 */
@RestController
@RequestMapping("/api/v1/tenants")
@RequiredArgsConstructor
public class TenantController {

    private final TenantService tenantService;

    /**
     * Registers a new tenant and its first ADMIN user in a single operation.
     * Returns the created tenant. The admin user's credentials can be used
     * immediately to call {@code POST /api/v1/auth/login}.
     *
     * @param request tenant name, slug, admin email, and admin password
     * @return {@code 201 Created} with the {@link TenantResponse} on success;
     *         {@code 409 Conflict} if the slug or admin email is already taken;
     *         {@code 400 Bad Request} if validation fails
     */
    @PostMapping("/register")
    public ResponseEntity<TenantResponse> register(@Valid @RequestBody RegisterTenantRequest request) {
        return ResponseEntity.status(HttpStatus.CREATED).body(tenantService.register(request));
    }

    /**
     * Returns the caller's own workspace.
     *
     * @param currentUser the authenticated user, injected from the JWT
     * @return {@code 200 OK} with the tenant; {@code 404} if it no longer exists
     */
    @GetMapping("/me")
    public ResponseEntity<TenantResponse> me(@AuthenticationPrincipal AuthenticatedUser currentUser) {
        return ResponseEntity.ok(tenantService.get(currentUser.tenantId()));
    }

    /**
     * Renames the caller's workspace. Admin only, enforced by
     * {@link com.log0.auth_service.security.SecurityConfig}.
     *
     * @param request     the new name
     * @param currentUser the authenticated admin, injected from the JWT
     * @return {@code 200 OK} with the updated tenant
     */
    @PatchMapping("/me")
    public ResponseEntity<TenantResponse> rename(
            @Valid @RequestBody UpdateTenantRequest request,
            @AuthenticationPrincipal AuthenticatedUser currentUser) {
        return ResponseEntity.ok(tenantService.rename(currentUser.tenantId(), request.getName()));
    }
}
