package com.log0.auth_service.exception;

/**
 * Thrown when a refresh token is rejected during the token refresh flow.
 * Mapped to {@code 401 Unauthorized} by {@link GlobalExceptionHandler}.
 *
 * <p>
 * Covers three cases: token not found in the database, token already
 * revoked (replayed after rotation), or token past its expiry date.
 */
public class InvalidTokenException extends RuntimeException {

    /**
     * @param message description of why the token was rejected
     *                (e.g. {@code "Refresh token has been revoked"})
     */
    public InvalidTokenException(String message) {
        super(message);
    }
}
