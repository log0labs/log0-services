package com.log0.ai_service.provider;

/**
 * Strategy interface for LLM provider implementations.
 * 
 * <p>Each implementation wraps a different LLM API (Groq, OpenAI, Gemini,
 * Anthropic) behind this single method. The active implementation is selected
 * at startup via {@code @ConditionalOnProperty(name = "ai.provider")} on each
 * concrete class - swapping providers requires only a config change, no code
 * changes elsewhere.
 *
 * <p>Implementations are responsible for their own HTTP client setup, request
 * serialization, response parsing, and error handling. Callers receive either
 * the generated summary text or a runtime exception if the provider fails.
 */
public interface LlmProvider {
    /**
     * Sends {@code prompt} to the configured LLM and returns the generated
     * summary text.
     *
     * @param prompt the fully-built prompt string from {@link com.log0.ai_service.prompt.PromptBuilder}
     * @return the raw summary text returned by the LLM
     * @throws RuntimeException if the provider API call fails or times out
     */
    String generateSummary(String prompt);
}
