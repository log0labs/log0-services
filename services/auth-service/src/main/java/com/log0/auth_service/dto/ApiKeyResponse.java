package com.log0.auth_service.dto;

import java.time.Instant;
import java.util.UUID;

import com.log0.auth_service.entity.ApiKey;

import lombok.Builder;
import lombok.Getter;

/**
 * Response body representing an API key.
 * Returned by key creation and key listing endpoints.
 *
 * <p>
 * The {@code rawKey} field is only populated at creation time — it contains the
 * plaintext key the client must copy immediately. On all subsequent requests
 * (e.g. listing keys), {@code rawKey} is {@code null} because the raw key is
 * never stored and cannot be recovered.
 */
@Getter
@Builder
public class ApiKeyResponse {

    private UUID keyId;
    private String name;
    private boolean active;
    private Instant createdAt;

    /**
     * The plaintext API key. Non-null only when the key is first created.
     * {@code null} on all subsequent reads — the raw key is shown once and
     * never stored.
     */
    private String rawKey;

    /**
     * Convenience factory method to build an {@link ApiKeyResponse} from an
     * {@link ApiKey} entity, without a raw key (used for listing existing keys).
     *
     * @param apiKey the entity to convert
     * @return a response DTO with {@code rawKey} set to {@code null}
     */
    public static ApiKeyResponse from(ApiKey apiKey) {
        return ApiKeyResponse.builder()
                .keyId(apiKey.getKeyId())
                .name(apiKey.getName())
                .active(apiKey.isActive())
                .createdAt(apiKey.getCreatedAt())
                .rawKey(null)
                .build();
    }

    /**
     * Convenience factory method to build an {@link ApiKeyResponse} from an
     * {@link ApiKey} entity with the raw key included.
     * Called only at key creation time, before the raw key is discarded.
     *
     * @param apiKey the newly created entity
     * @param rawKey the plaintext key to return to the client
     * @return a response DTO with the raw key populated
     */
    public static ApiKeyResponse from(ApiKey apiKey, String rawKey) {
        return ApiKeyResponse.builder()
                .keyId(apiKey.getKeyId())
                .name(apiKey.getName())
                .active(apiKey.isActive())
                .createdAt(apiKey.getCreatedAt())
                .rawKey(rawKey)
                .build();
    }
}
