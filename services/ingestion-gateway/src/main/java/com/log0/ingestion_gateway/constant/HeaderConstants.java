package com.log0.ingestion_gateway.constant;

/**
 * Defines the canonical HTTP header names expected on every log ingestion request.
 * All headers are required; absence of any header causes the request to be rejected
 * by {@link com.log0.ingestion_gateway.utils.RequestHeaderExtractor}.
 */
public class HeaderConstants {
    private HeaderConstants() {
        // Private constructor to prevent instantiation
    }

    public static final String API_KEY = "X-API-KEY";
    public static final String TENANT_ID = "X-TENANT-ID";
    public static final String SERVICE_NAME = "X-SERVICE-NAME";
    public static final String ENVIRONMENT = "X-ENVIRONMENT";
}
