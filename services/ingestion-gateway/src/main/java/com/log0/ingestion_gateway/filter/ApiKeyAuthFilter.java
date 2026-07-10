package com.log0.ingestion_gateway.filter;

import java.io.IOException;

import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import com.log0.ingestion_gateway.auth.ApiKeyValidator;
import com.log0.ingestion_gateway.constant.HeaderConstants;
import com.log0.ingestion_gateway.exception.UnauthorizedException;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

/**
 * Authenticates every {@code /api/v1/logs} request by its API key BEFORE it
 * reaches the controller. On success, the resolved tenantId is placed on the
 * request as {@link #TENANT_ATTRIBUTE} - the controller reads it from there and
 * never trusts a client-supplied tenant header.
 */
@Component
public class ApiKeyAuthFilter extends OncePerRequestFilter {
    public static final String TENANT_ATTRIBUTE = "validatedTenantId";

    private final ApiKeyValidator apiKeyValidator;

    public ApiKeyAuthFilter(ApiKeyValidator apiKeyValidator) {
        this.apiKeyValidator = apiKeyValidator;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        return !request.getRequestURI().startsWith("/api/v1/logs");
    }

     @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain) throws ServletException, IOException {

        String apiKey = request.getHeader(HeaderConstants.API_KEY);
        if (apiKey == null || apiKey.isBlank()) {
            reject(response, "Missing " + HeaderConstants.API_KEY + " header");
            return;
        }

        try {
            String tenantId = apiKeyValidator.resolveTenantId(apiKey);
            request.setAttribute(TENANT_ATTRIBUTE, tenantId);
        } catch (UnauthorizedException e) {
            reject(response, e.getMessage());
            return;
        }

        filterChain.doFilter(request, response);
    }

    private void reject(HttpServletResponse response, String message) throws IOException {
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
        response.setContentType("application/json");
        response.getWriter().write("{\"error\":\"" + message + "\"}");
    }
}
