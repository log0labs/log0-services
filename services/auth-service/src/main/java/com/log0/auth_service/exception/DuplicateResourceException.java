package com.log0.auth_service.exception;

/**
 * Thrown when a create operation would produce a duplicate unique value.
 * Mapped to {@code 409 Conflict} by {@link GlobalExceptionHandler}.
 *
 * <p>
 * Example: registering a tenant with a slug that is already taken,
 * or creating a user with an email that already exists.
 */
public class DuplicateResourceException extends RuntimeException {

    /**
     * @param message description of the conflict (e.g.
     *                {@code "Slug already taken: acme-corp"})
     */
    public DuplicateResourceException(String message) {
        super(message);
    }
}
