package com.log0.auth_service.service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.log0.auth_service.dto.ApiKeyResponse;
import com.log0.auth_service.dto.CreateApiKeyRequest;
import com.log0.auth_service.entity.ApiKey;
import com.log0.auth_service.entity.Tenant;
import com.log0.auth_service.exception.ResourceNotFoundException;
import com.log0.auth_service.repository.ApiKeyRepository;
import com.log0.auth_service.repository.TenantRepository;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Handles API key lifecycle for a tenant: generation, listing, and revocation.
 * Also provides the key validation endpoint called by the ingestion gateway.
 *
 * <p>
 * Raw key values are never stored. Only the SHA-256 hash of the key is
 * persisted. The raw key is returned once at creation time and cannot be
 * recovered afterwards.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ApiKeyService {

    private final ApiKeyRepository apiKeyRepository;
    private final TenantRepository tenantRepository;

    /**
     * Generates a new API key for the given tenant, stores its SHA-256 hash,
     * and returns the raw key in the response.
     * The raw key is a cryptographically random 32-byte value encoded as a
     * 64-character hex string, prefixed with {@code log0_} for easy identification.
     *
     * @param tenantId the tenant to generate the key for, from the caller's JWT
     * @param request  contains the human-readable label for the key
     * @return an {@link ApiKeyResponse} with the raw key populated - this is the
     *         only time the raw key will ever be visible
     * @throws ResourceNotFoundException if the tenant does not exist
     */
    @Transactional
    public ApiKeyResponse createApiKey(UUID tenantId, CreateApiKeyRequest request) {
        Tenant tenant = tenantRepository.findById(tenantId)
                .orElseThrow(() -> new ResourceNotFoundException("Tenant not found: " + tenantId));

        String rawKey = generateRawKey();
        String keyHash = sha256(rawKey);

        ApiKey apiKey = new ApiKey();
        apiKey.setTenant(tenant);
        apiKey.setName(request.getName());
        apiKey.setKeyHash(keyHash);
        apiKeyRepository.save(apiKey);

        log.info("Created API key {} for tenant {}", apiKey.getKeyId(), tenantId);

        return ApiKeyResponse.from(apiKey, rawKey);
    }

    /**
     * Returns all API keys belonging to the given tenant.
     * The raw key is not included - it is never stored and cannot be recovered.
     *
     * @param tenantId the tenant whose keys to list, from the caller's JWT
     * @return list of API keys for the tenant, active and revoked
     */
    public List<ApiKeyResponse> listApiKeys(UUID tenantId) {
        return apiKeyRepository.findByTenant_TenantId(tenantId).stream()
                .map(ApiKeyResponse::from)
                .toList();
    }

    /**
     * Revokes an API key by setting it inactive. The record is retained for
     * audit purposes. Revoked keys are rejected immediately by
     * {@link #validateApiKey(String)}.
     *
     * @param keyId    the key to revoke
     * @param tenantId the tenant that owns the key, used to prevent cross-tenant
     *                 revocation
     * @throws ResourceNotFoundException if no active key with this ID exists under
     *                                   the tenant
     */
    @Transactional
    public void revokeApiKey(UUID keyId, UUID tenantId) {
        ApiKey apiKey = apiKeyRepository.findById(keyId)
                .filter(k -> k.getTenant().getTenantId().equals(tenantId))
                .orElseThrow(() -> new ResourceNotFoundException("API key not found: " + keyId));

        apiKey.setActive(false);
        apiKeyRepository.save(apiKey);

        log.info("Revoked API key {} for tenant {}", keyId, tenantId);
    }

    /**
     * Validates an API key presented by the ingestion gateway.
     * Hashes the raw key and looks up the matching active record.
     * If found, updates {@code lastUsedAt} and returns the owning tenant's ID.
     *
     * @param rawKey the plaintext API key from the request header
     * @return the tenantId that owns the key, to be forwarded to the ingestion
     *         pipeline
     * @throws ResourceNotFoundException if no active key matches the hash
     */
    @Transactional
    public UUID validateApiKey(String rawKey) {
        String keyHash = sha256(rawKey);

        ApiKey apiKey = apiKeyRepository.findByKeyHashAndActiveTrue(keyHash)
                .orElseThrow(() -> new ResourceNotFoundException("Invalid or inactive API key"));

        apiKey.setLastUsedAt(java.time.Instant.now());
        apiKeyRepository.save(apiKey);

        return apiKey.getTenant().getTenantId();
    }

    /**
     * Generates a cryptographically random API key.
     * Uses {@link SecureRandom} to produce 32 random bytes, encodes them as
     * a 64-character hex string, and prepends {@code log0_} as a recognisable
     * prefix (e.g. {@code log0_3f2a1b...}).
     *
     * @return a unique, unpredictable raw key string
     */
    private String generateRawKey() {
        byte[] bytes = new byte[32];
        new SecureRandom().nextBytes(bytes);
        return "log0_" + HexFormat.of().formatHex(bytes);
    }

    /**
     * Computes the SHA-256 hash of the given string and returns it as a
     * 64-character lowercase hex string.
     *
     * @param input the string to hash
     * @return the hex-encoded SHA-256 digest
     * @throws IllegalStateException if the SHA-256 algorithm is unavailable
     *                               (guaranteed present on all JVMs)
     */
    private String sha256(String input) {
        try {
            byte[] hash = MessageDigest.getInstance("SHA-256")
                    .digest(input.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(hash);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 not available", e);
        }
    }
}
