package com.log0.auth_service.security;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.security.authentication.AuthenticationManager;
import org.springframework.security.config.annotation.authentication.configuration.AuthenticationConfiguration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

import lombok.RequiredArgsConstructor;

/**
 * Spring Security configuration for auth-service.
 * Defines the security filter chain, session policy, endpoint access rules,
 * and core security beans ({@link PasswordEncoder},
 * {@link AuthenticationManager}).
 *
 * <p>
 * The service is fully stateless - no HTTP session is created or used.
 * Authentication state lives in the JWT, validated per-request by
 * {@link JwtAuthFilter}.
 */
@Configuration
@EnableWebSecurity
@RequiredArgsConstructor
public class SecurityConfig {

    private final JwtAuthFilter jwtAuthFilter;

    /**
     * Configures the security filter chain:
     * <ul>
     * <li>CSRF disabled - not needed for stateless JWT APIs</li>
     * <li>Stateless session - no server-side session is created</li>
     * <li>Public endpoints: registration, login, refresh, API key validation,
     * actuator health</li>
     * <li>ADMIN-only endpoints: user management, API key management</li>
     * <li>All other requests require authentication</li>
     * </ul>
     *
     * @param http the {@link HttpSecurity} builder provided by Spring
     * @return the configured {@link SecurityFilterChain}
     */
    @Bean
    public SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        return http
                .csrf(AbstractHttpConfigurer::disable)
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(HttpMethod.POST, "/api/v1/tenants/register").permitAll()
                        .requestMatchers(HttpMethod.POST, "/api/v1/auth/login").permitAll()
                        .requestMatchers(HttpMethod.POST, "/api/v1/auth/refresh").permitAll()
                        .requestMatchers(HttpMethod.POST, "/api/v1/auth/validate-key").permitAll()
                        .requestMatchers("/actuator/health").permitAll()
                        .requestMatchers("/api/v1/users/**").hasRole("ADMIN")
                        .requestMatchers("/api/v1/api-keys/**").hasRole("ADMIN")
                        .anyRequest().authenticated())
                .addFilterBefore(jwtAuthFilter, UsernamePasswordAuthenticationFilter.class)
                .build();
    }

    /**
     * BCrypt password encoder used for hashing and verifying user passwords.
     * The default strength factor (10) provides a good balance between security
     * and performance - each hash takes ~100ms to compute, making brute-force
     * attacks expensive.
     *
     * @return a {@link BCryptPasswordEncoder} with default strength
     */
    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder();
    }

    /**
     * Exposes Spring's {@link AuthenticationManager} as a bean so it can be
     * injected into services that need to programmatically authenticate users.
     *
     * @param config the authentication configuration auto-configured by Spring
     * @return the application's {@link AuthenticationManager}
     */
    @Bean
    public AuthenticationManager authenticationManager(
            AuthenticationConfiguration config) throws Exception {
        return config.getAuthenticationManager();
    }
}
