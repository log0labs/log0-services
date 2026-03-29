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
 * LLM provider implementation that calls the Anthropic Messages API.
 *
 * <p>
 * Anthropic has its own API format - authentication uses a custom
 * {@code x-api-key} header (not {@code Authorization: Bearer}), the system
 * prompt is a top-level string field (not a message in the array), and the
 * response is in {@code content[0].text} (not
 * {@code choices[0].message.content}).
 * A mandatory {@code anthropic-version} header must be sent with every request.
 *
 * <p>
 * This bean is only created when {@code ai.provider=anthropic} in
 * {@code application.yml}. Requires an Anthropic API key in
 * {@code ANTHROPIC_API_KEY}.
 */
@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "ai.provider", havingValue = "anthropic")
public class AnthropicProvider implements LlmProvider {

    private static final String BASE_URL = "https://api.anthropic.com";
    private static final String ANTHROPIC_VERSION = "2023-06-01";
    private static final double TEMPERATURE = 0.2;
    private static final int MAX_TOKENS = 300;

    private final AiConfig aiConfig;

    private RestClient restClient;

    /**
     * Initialises the {@link RestClient} with the Anthropic base URL,
     * {@code x-api-key} and {@code anthropic-version} headers, and connect
     * timeout from the {@code ai.anthropic} config block.
     */
    @PostConstruct
    private void init() {
        AiConfig.ProviderConfig config = aiConfig.getAnthropic();

        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(config.getTimeoutSeconds()))
                .build();

        this.restClient = RestClient.builder()
                .baseUrl(BASE_URL)
                .requestFactory(new JdkClientHttpRequestFactory(httpClient))
                .defaultHeader("x-api-key", config.getApiKey())
                .defaultHeader("anthropic-version", ANTHROPIC_VERSION)
                .build();
    }

    /**
     * Sends {@code prompt} to the Anthropic {@code /v1/messages} endpoint and
     * returns the generated text from the first content block.
     *
     * <p>
     * The system prompt is passed as a top-level {@code system} field, not as
     * a message inside the {@code messages} array. Only the {@code user} role
     * is used in the messages array - Anthropic does not accept a
     * {@code role:system} entry there.
     *
     * @param prompt the fully-built incident context prompt from {@link com.log0.ai_service.prompt.PromptBuilder}
     * @return the raw summary text from the model
     * @throws org.springframework.web.client.RestClientException if the API call
     *                                                            fails or returns a
     *                                                            non-2xx status
     */
    @Override
    public String generateSummary(String prompt) {
        String model = aiConfig.getAnthropic().getModel();
        log.debug("Calling Anthropic API with model={}", model);

        Map<String, Object> requestBody = Map.of(
                "model", model,
                "system", systemPrompt(),
                "messages", List.of(
                        Map.of("role", "user", "content", prompt)),
                "temperature", TEMPERATURE,
                "max_tokens", MAX_TOKENS);

        AnthropicResponse response = restClient.post()
                .uri("/v1/messages")
                .body(requestBody)
                .retrieve()
                .body(AnthropicResponse.class);

        return response.content().get(0).text();
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

    // Inner response types (Anthropic shape)

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record AnthropicResponse(List<ContentBlock> content) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record ContentBlock(String type, String text) {
    }
}
