package com.log0.ingestion_gateway.utils;

import jakarta.servlet.http.HttpServletRequest;

/**
 * Utility for reading mandatory HTTP headers from an incoming servlet request.
 * Centralises the null/empty check so controllers stay free of boilerplate
 * header-validation logic.
 */
public class RequestHeaderExtractor {
    private RequestHeaderExtractor() {
        // Private constructor to prevent instantiation
    }

    /**
     * Returns the value of the named header, or throws if it is absent or blank.
     *
     * @param request    the current HTTP request
     * @param headerName the exact header name to look up (case-insensitive per HTTP spec)
     * @return the non-empty header value
     * @throws IllegalArgumentException if the header is missing or empty, which is
     *                                  mapped to a 400 response by the global exception handler
     */
    public static String getRequiredHeader(HttpServletRequest request, String headerName) {
        String value = request.getHeader(headerName);

        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException("Missing required header: " + headerName);
        }

        return value;
    }
}
