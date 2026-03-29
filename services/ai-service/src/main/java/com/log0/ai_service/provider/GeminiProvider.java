package com.log0.ai_service.provider;

import java.net.http.HttpClient;
import java.time.Duration;
import java.util.List;
import java.util.Map;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.log0.ai_service.config.AiConfig;

import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * LLM provider implementation that calls the Google Gemini API.
 *
 * <p>
 * Unlike {@link GroqProvider} and {@link OpenAiProvider}, Gemini uses a
 * different authentication method (API key as a query parameter instead of an
 * Authorization header), a different endpoint shape (model name embedded in
 * the URI), and a different request and response format.
 *
 * <p>
 * This bean is only created when {@code ai.provider=gemini} in
 * {@code application.yml}. Uses the free {@code gemini-1.5-flash} model by
 * default, configurable via {@code ai.gemini.model}.
 */
@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "ai.provider", havingValue = "gemini")
public class GeminiProvider implements LlmProvider {

    private static final String BASE_URL = "https://generativelanguage.googleapis.com";
    private static final double TEMPERATURE = 0.2;
    private static final int MAX_TOKENS = 300;

    private final AiConfig aiConfig;

    private RestClient restClient;
    private String apiKey;

    /**
     * Initialises the {@link RestClient} with the Gemini base URL and connect
     * timeout. The API key is stored separately because Gemini passes it as a
     * URI query parameter, not an Authorization header.
     */
    @PostConstruct
    private void init() {
        AiConfig.ProviderConfig config = aiConfig.getGemini();
        this.apiKey = config.getApiKey();

        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(config.getTimeoutSeconds()))
                .build();

        this.restClient = RestClient.builder()
                .baseUrl(BASE_URL)
                .requestFactory(new JdkClientHttpRequestFactory(httpClient))
                .build();
    }

    /**
     * Sends {@code prompt} to the Gemini {@code generateContent} endpoint and
     * returns the generated text from the first candidate.
     *
     * <p>
     * The model name is embedded in the URI path. The system instruction is
     * passed via the {@code systemInstruction} field rather than as a
     * {@code role:system} message, which is the Gemini-native approach.
     *
     * @param prompt the fully-built incident context prompt from {@link com.log0.ai_service.prompt.PromptBuilder}
     * @return the raw summary text from the model
     * @throws org.springframework.web.client.RestClientException if the API call
     *                                                            fails or returns a
     *                                                            non-2xx status
     */
    @Override
    public String generateSummary(String prompt) {
        String model = aiConfig.getGemini().getModel();
        log.debug("Calling Gemini API with model={}", model);

        Map<String, Object> requestBody = Map.of(
                "systemInstruction", Map.of(
                        "parts", List.of(Map.of("text", systemPrompt()))),
                "contents", List.of(
                        Map.of("parts", List.of(Map.of("text", prompt)))),
                "generationConfig", Map.of(
                        "temperature", TEMPERATURE,
                        "maxOutputTokens", MAX_TOKENS));

        GeminiResponse response = restClient.post()
                .uri("/v1beta/models/{model}:generateContent?key={key}", model, apiKey)
                .body(requestBody)
                .retrieve()
                .body(GeminiResponse.class);

        return response.candidates().get(0).content().parts().get(0).text();
    }

    private String systemPrompt() {
        return """
                You are an incident analysis assistant for a software engineering team.
                Given production incident data, write a concise structured summary.
                Respond ONLY in this exact format - no extra text:

                Summary: <1 sentence describing what is failing>
                Possible Cause: <1-2 sentences using conditional language: "may be", "likely", "possibly">
                Recommended Actions:
                - <action 1>
                - <action 2>
                """;
    }

    // Inner response types (Gemini shape)

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record GeminiResponse(List<Candidate> candidates) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record Candidate(Content content) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record Content(List<Part> parts) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record Part(String text) {
    }
}
