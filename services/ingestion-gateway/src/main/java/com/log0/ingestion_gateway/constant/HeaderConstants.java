package com.log0.ingestion_gateway.constant;

/**
 * Defines the canonical HTTP header names expected on every log ingestion request.
 * {@code X-API-KEY} is validated by
 * {@link com.log0.ingestion_gateway.filter.ApiKeyAuthFilter}, which derives the tenant;
 * {@code X-SERVICE-NAME} and {@code X-ENVIRONMENT} are read and required by the
 * controller via {@link com.log0.ingestion_gateway.utils.RequestHeaderExtractor}.
 * The tenant is never accepted from a client header.
 */
public class HeaderConstants {
    private HeaderConstants() {
        // Private constructor to prevent instantiation
    }

    public static final String API_KEY = "X-API-KEY";
    public static final String SERVICE_NAME = "X-SERVICE-NAME";
    public static final String ENVIRONMENT = "X-ENVIRONMENT";
}
