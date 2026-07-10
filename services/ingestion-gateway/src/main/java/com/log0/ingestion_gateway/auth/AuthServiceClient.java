package com.log0.ingestion_gateway.auth;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatusCode;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import com.log0.ingestion_gateway.constant.HeaderConstants;
import com.log0.ingestion_gateway.exception.UnauthorizedException;

/**
 * Thin client over auth-service's public key-validation endpoint.
 * Called only on a cache miss (see {@link ApiKeyValidator}), so it is not on
 * the per-request hot path once a key is warm.
 */
@Component
public class AuthServiceClient {
    private final RestClient restClient;

    public AuthServiceClient(@Value("${auth-service.base-url}") String baseUrl) {
        // RestClient.create avoids depending on an auto-configured RestClient.Builder
        // bean, which is not present in this webmvc-only setup.
        this.restClient = RestClient.create(baseUrl);
    }

    /** Returns the owning tenantId, or throws {@link UnauthorizedException} if the key is invalid/revoked. */
    public String validateKey(String apiKey) {
        ValidateKeyResponse body = restClient.post()
                .uri("/api/v1/auth/validate-key")
                .header("X-Api-Key", apiKey)
                .retrieve()
                .onStatus(HttpStatusCode::is4xxClientError, (req, res) -> {
                    throw new UnauthorizedException("Invalid or revoked API key");
                })
                .body(ValidateKeyResponse.class);

        if (body == null || body.tenantId() == null) {
            throw new UnauthorizedException("Empty validation response");
        }

        return body.tenantId();
    }

    /** Shape of the auth-service {@code /validate-key} success body: {@code {"tenantId":"<uuid>"}}. */
    private record ValidateKeyResponse(String tenantId) {}
}
