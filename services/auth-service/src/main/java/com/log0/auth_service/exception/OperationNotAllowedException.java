package com.log0.auth_service.exception;

/**
 * Thrown when a request is valid but would break a rule, such as removing the last
 * administrator or changing your own role. Mapped to {@code 409 Conflict}.
 */
public class OperationNotAllowedException extends RuntimeException {

    /**
     * @param message what is not allowed and why, safe to show to the caller
     */
    public OperationNotAllowedException(String message) {
        super(message);
    }
}
