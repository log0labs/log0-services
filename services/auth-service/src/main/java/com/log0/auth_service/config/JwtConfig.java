package com.log0.auth_service.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;
import lombok.Getter;
import lombok.Setter;

/**
 * Binds all properties under the {@code jwt} prefix in {@code application.yml}
 * to a typed Java object. Spring Boot automatically populates this on startup
 * via {@code @ConfigurationPropertiesScan} on the main application class.
 *
 * <p>
 * {@code @Validated} causes Spring Boot to validate the bound values immediately
 * at startup. If any constraint is violated (e.g. {@code JWT_SECRET} env var is
 * missing), the application refuses to start with a clear error message rather
 * than failing later with a cryptic {@code NullPointerException}.
 *
 * <p>
 * Usage in other beans: inject {@code JwtConfig} directly rather than using
 * individual {@code @Value} fields - this keeps JWT configuration in one place
 * and makes it easy to add new settings later.
 */
@Getter
@Setter
@Validated
@ConfigurationProperties(prefix = "jwt")
public class JwtConfig {

    /**
     * The HMAC-SHA256 secret key used to sign and verify JWT tokens.
     * Must be at least 32 characters long for HS256.
     * Read from the {@code JWT_SECRET} environment variable via
     * {@code ${JWT_SECRET}} in {@code application.yml}.
     */
    @NotBlank
    private String secret;

    /**
     * How long (in seconds) an access token stays valid after issuance.
     * Default from {@code application.yml}: 3600 (1 hour).
     * After expiry the client must use the refresh token to get a new one.
     */
    @Positive
    private long accessTokenExpirySeconds;

    /**
     * How long (in days) a refresh token stays valid after issuance.
     * Default from {@code application.yml}: 30 days.
     * Refresh tokens are rotated on each use - a token is invalidated the
     * moment it is redeemed.
     */
    @Positive
    private long refreshTokenExpiryDays;
}
