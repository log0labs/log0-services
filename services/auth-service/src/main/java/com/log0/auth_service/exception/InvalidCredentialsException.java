package com.log0.auth_service.exception;

/**
 * Thrown when a login attempt fails due to an unrecognised email
 * or an incorrect password.
 * Mapped to {@code 401 Unauthorized} by {@link GlobalExceptionHandler}.
 *
 * <p>
 * The message intentionally does not reveal whether the email exists —
 * both cases return the same generic message to prevent user enumeration.
 */
public class InvalidCredentialsException extends RuntimeException {

    public InvalidCredentialsException() {
        super("Invalid email or password");
    }
}
