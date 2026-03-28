package com.log0.ingestion_gateway.exception;

import java.util.List;

/**
 * JSON error envelope returned by {@link GlobalExceptionHandler} for all 4xx/5xx responses.
 *
 * <p>{@code error} is a short human-readable message; {@code details} carries
 * per-field validation errors (may be null for non-validation errors).
 */
public class ErrorResponse {
    private final String error;
    private final List<String> details;

    public ErrorResponse(String error, List<String> details) {
        this.error = error;
        this.details = details;
    }

    public String getError() {
        return error;
    }

    public List<String> getDetails() {
        return details;
    }
}
