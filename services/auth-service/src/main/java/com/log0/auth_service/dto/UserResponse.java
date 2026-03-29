package com.log0.auth_service.dto;

import java.time.Instant;
import java.util.UUID;

import com.log0.auth_service.entity.User;
import com.log0.auth_service.entity.User.Role;

import lombok.Builder;
import lombok.Getter;

/**
 * Response body representing a user.
 * Returned by user creation and user listing endpoints.
 * The {@code passwordHash} field from the entity is intentionally excluded —
 * password data must never appear in API responses.
 */
@Getter
@Builder
public class UserResponse {

    private UUID userId;
    private String email;
    private Role role;
    private Instant createdAt;

    /**
     * Convenience factory method to build a {@link UserResponse} from a
     * {@link User} entity.
     *
     * @param user the entity to convert
     * @return a response DTO populated from the entity
     */
    public static UserResponse from(User user) {
        return UserResponse.builder()
                .userId(user.getUserId())
                .email(user.getEmail())
                .role(user.getRole())
                .createdAt(user.getCreatedAt())
                .build();
    }
}
