package com.log0.ingestion_gateway.auth;

import org.springframework.cache.annotation.Cacheable;
import org.springframework.stereotype.Service;

/**
 * Resolves an API key to its tenantId, caching successful results so the auth
 * network call happens at most once per key per TTL window. Failures are NOT
 * cached (a rejected key must be re-checked, and revoked keys expire naturally).
 */
@Service
public class ApiKeyValidator {
    private final AuthServiceClient authServiceClient;

    public ApiKeyValidator(AuthServiceClient authServiceClient) {
        this.authServiceClient = authServiceClient;
    }

    @Cacheable(cacheNames = "apiKeyTenant", key = "#apiKey")
    public String resolveTenantId(String apiKey) {
        return authServiceClient.validateKey(apiKey);
    }
}
