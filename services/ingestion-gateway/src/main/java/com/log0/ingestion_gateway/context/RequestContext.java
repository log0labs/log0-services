package com.log0.ingestion_gateway.context;

/**
 * Immutable value object that carries per-request identity metadata extracted from
 * HTTP headers, threaded through the ingestion pipeline so downstream components
 * (service, producer) do not need to re-read the servlet request.
 */
public class RequestContext {

    private final String tenantId;
    private final String serviceName;
    private final String environment;
    private final String apiKey;

    public RequestContext(
            String tenantId,
            String serviceName,
            String environment,
            String apiKey) {
        this.tenantId = tenantId;
        this.serviceName = serviceName;
        this.environment = environment;
        this.apiKey = apiKey;
    }

    public String getTenantId() {
        return tenantId;
    }

    public String getServiceName() {
        return serviceName;
    }

    public String getEnvironment() {
        return environment;
    }

    public String getApiKey() {
        return apiKey;
    }
}
