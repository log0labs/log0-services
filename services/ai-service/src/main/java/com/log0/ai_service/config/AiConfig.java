package com.log0.ai_service.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import lombok.Getter;
import lombok.Setter;

/**
 * Binds all properties under the {@code ai} prefix in {@code application.yml}
 * to a typed Java object. Spring Boot automatically populates this on startup
 * via {@code @ConfigurationPropertiesScan} on the main application class.
 *
 * <p>
 * The active LLM provider is selected by {@code ai.provider}. Each provider
 * has its own nested config block ({@code ai.groq}, {@code ai.openai}, etc.)
 * holding its API key, model name, and timeout. Only the active provider's
 * block needs to be filled in - the others can remain empty.
 */
@Getter
@Setter
@ConfigurationProperties(prefix = "ai")
public class AiConfig {
    /**
     * Selects the active LLM provider at startup.
     * Valid values: {@code groq}, {@code openai}, {@code gemini}, {@code anthropic}.
     * Matches the {@code @ConditionalOnProperty} value on each provider bean.
     */
    private String provider;

    /**
     * Base URL of the Incident Service used for the AI summary callback.
     * Example: {@code http://localhost:8083}
     */
    private String incidentServiceBaseUrl;

    private ProviderConfig groq = new ProviderConfig();
    private ProviderConfig openai = new ProviderConfig();
    private ProviderConfig gemini = new ProviderConfig();
    private ProviderConfig anthropic = new ProviderConfig();

    /**
     * Shared config block for a single LLM provider - API key, model name,
     * and request timeout. Each provider (Groq, OpenAI, etc.) has its own
     * instance of this class under the {@code ai.<provider>} prefix.
     */
    @Getter
    @Setter
    public static class ProviderConfig {

        /** API key for authenticating with the provider's REST API. */
        private String apiKey;

        /** Model identifier to request (e.g. {@code llama-3.3-70b-versatile}). */
        private String model;

        /**
         * Maximum seconds to wait for the LLM response before giving up.
         * Defaults to 30. LLM APIs can be slow - don't set this too low.
         */
        private int timeoutSeconds = 30;
    }
}
