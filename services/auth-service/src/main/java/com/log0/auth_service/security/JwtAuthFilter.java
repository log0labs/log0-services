package com.log0.auth_service.security;

import java.io.IOException;
import java.util.List;

import org.springframework.lang.NonNull;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import com.log0.auth_service.service.JwtUtil;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Servlet filter that validates the JWT Bearer token on every incoming request.
 * Runs exactly once per request (guaranteed by {@link OncePerRequestFilter}).
 *
 * <p>
 * If a valid JWT is present, an {@link AuthenticatedUser} is built from its
 * claims and placed into Spring Security's {@code SecurityContext}. Spring
 * Security then uses this to decide whether the request is allowed to proceed
 * based on the endpoint's access rules in {@link SecurityConfig}.
 *
 * <p>
 * If the token is missing, malformed, or expired, the filter does nothing -
 * it does not reject the request itself. Spring Security's authorization layer
 * handles rejection: protected endpoints will return {@code 401} because no
 * authentication is present in the context.
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class JwtAuthFilter extends OncePerRequestFilter {

    private static final String BEARER_PREFIX = "Bearer ";

    private final JwtUtil jwtUtil;

    /**
     * Extracts and validates the JWT from the {@code Authorization} header.
     * On success, sets the authenticated principal in the {@code SecurityContext}.
     * On failure (missing header, bad token), clears the context and continues
     * the filter chain - the authorization layer will reject the request if the
     * endpoint requires authentication.
     *
     * @param request     the incoming HTTP request
     * @param response    the outgoing HTTP response
     * @param filterChain the remaining filters to execute after this one
     */
    @Override
    protected void doFilterInternal(@NonNull HttpServletRequest request,
            @NonNull HttpServletResponse response,
            @NonNull FilterChain filterChain)
            throws ServletException, IOException {

        String authHeader = request.getHeader("Authorization");

        if (authHeader == null || !authHeader.startsWith(BEARER_PREFIX)) {
            filterChain.doFilter(request, response);
            return;
        }

        String token = authHeader.substring(BEARER_PREFIX.length());

        try {
            Claims claims = jwtUtil.parseToken(token);

            AuthenticatedUser principal = new AuthenticatedUser(
                    jwtUtil.extractUserId(claims),
                    jwtUtil.extractTenantId(claims),
                    jwtUtil.extractRole(claims));

            SimpleGrantedAuthority authority = new SimpleGrantedAuthority("ROLE_" + principal.role().name());

            UsernamePasswordAuthenticationToken authentication = new UsernamePasswordAuthenticationToken(principal,
                    null, List.of(authority));

            SecurityContextHolder.getContext().setAuthentication(authentication);

        } catch (JwtException e) {
            log.debug("Invalid JWT token: {}", e.getMessage());
            SecurityContextHolder.clearContext();
        }

        filterChain.doFilter(request, response);
    }
}
