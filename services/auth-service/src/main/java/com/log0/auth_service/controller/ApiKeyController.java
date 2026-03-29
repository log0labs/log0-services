package com.log0.auth_service.controller;

import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.log0.auth_service.dto.ApiKeyResponse;
import com.log0.auth_service.dto.CreateApiKeyRequest;
import com.log0.auth_service.security.AuthenticatedUser;
import com.log0.auth_service.service.ApiKeyService;

import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;

/**
 * REST controller exposing API key management under {@code /api/v1/api-keys}.
 * All endpoints require the {@code ADMIN} role, enforced by
 * {@link com.log0.auth_service.security.SecurityConfig}.
 *
 * <p>
 * A separate endpoint at {@code POST /api/v1/auth/validate-key} handles
 * machine-to-machine key validation for the ingestion gateway - that endpoint
 * is public and lives in {@link AuthController}.
 */
@RestController
@RequestMapping("/api/v1/api-keys")
@RequiredArgsConstructor
public class ApiKeyController {

    private final ApiKeyService apiKeyService;

    /**
     * Generates a new API key for the authenticated admin's tenant.
     * The raw key is returned once in the response - it is never stored
     * and cannot be retrieved again.
     *
     * @param request     the human-readable label for the key
     * @param currentUser the authenticated admin, injected from the JWT
     * @return {@code 201 Created} with the {@link ApiKeyResponse} including the raw
     *         key
     */
    @PostMapping
    public ResponseEntity<ApiKeyResponse> createApiKey(
            @Valid @RequestBody CreateApiKeyRequest request,
            @AuthenticationPrincipal AuthenticatedUser currentUser) {
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(apiKeyService.createApiKey(currentUser.tenantId(), request));
    }

    /**
     * Lists all API keys for the authenticated admin's tenant.
     * The raw key is not included in the response.
     *
     * @param currentUser the authenticated admin, injected from the JWT
     * @return {@code 200 OK} with a list of {@link ApiKeyResponse} objects
     */
    @GetMapping
    public ResponseEntity<List<ApiKeyResponse>> listApiKeys(
            @AuthenticationPrincipal AuthenticatedUser currentUser) {
        return ResponseEntity.ok(apiKeyService.listApiKeys(currentUser.tenantId()));
    }

    /**
     * Revokes an API key by setting it inactive.
     * The key record is retained for audit purposes but will be rejected
     * on all future validation attempts.
     *
     * @param keyId       the ID of the key to revoke
     * @param currentUser the authenticated admin, injected from the JWT
     * @return {@code 204 No Content} on success;
     *         {@code 404} if the key does not exist under the admin's tenant
     */
    @DeleteMapping("/{keyId}")
    public ResponseEntity<Void> revokeApiKey(
            @PathVariable UUID keyId,
            @AuthenticationPrincipal AuthenticatedUser currentUser) {
        apiKeyService.revokeApiKey(keyId, currentUser.tenantId());
        return ResponseEntity.noContent().build();
    }
}
