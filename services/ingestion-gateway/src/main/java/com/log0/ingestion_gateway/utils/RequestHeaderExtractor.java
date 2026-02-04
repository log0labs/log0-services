package com.log0.ingestion_gateway.utils;

import jakarta.servlet.http.HttpServletRequest;

public class RequestHeaderExtractor {
    private RequestHeaderExtractor() {
        // Private constructor to prevent instantiation
    }

    public static String getRequiredHeader(HttpServletRequest request, String headerName) {
        String value = request.getHeader(headerName);

        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException("Missing required header: " + headerName);
        }

        return value;
    }
}
