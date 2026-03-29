package com.log0.auth_service.exception;

/**
 * Thrown when a requested resource does not exist in the database.
 * Mapped to {@code 404 Not Found} by {@link GlobalExceptionHandler}.
 *
 * <p>
 * Example: looking up a user by ID that does not exist, or finding
 * an API key that has already been deleted.
 */
public class ResourceNotFoundException extends RuntimeException {

    /**
     * @param message description of what was not found
     *                (e.g. {@code "User not found: <id>"})
     */
    public ResourceNotFoundException(String message) {
        super(message);
    }
}
