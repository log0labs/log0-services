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
 * LLM provider implementation that calls the OpenAI API.
 *
 * <p>
 * Uses the {@code /v1/chat/completions} endpoint with the model configured
 * under {@code ai.openai.model} (e.g. {@code gpt-4o}). The request and
 * response shape is identical to {@link GroqProvider} - OpenAI defined this
 * format and Groq copied it for compatibility.
 *
 * <p>
 * This bean is only created when {@code ai.provider=openai} in
 * {@code application.yml}. Requires a paid OpenAI API key in
 * {@code OPENAI_API_KEY}.
 */
@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "ai.provider", havingValue = "openai")
public class OpenAiProvider implements LlmProvider {

    private static final String BASE_URL = "https://api.openai.com";
    private static final double TEMPERATURE = 0.2;
    private static final int MAX_TOKENS = 300;

    private final AiConfig aiConfig;

    private RestClient restClient;

    /**
     * Initialises the {@link RestClient} with the OpenAI base URL,
     * Authorization header, and connect timeout from the {@code ai.openai}
     * config block.
     */
    @PostConstruct
    private void init() {
        AiConfig.ProviderConfig config = aiConfig.getOpenai();

        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(config.getTimeoutSeconds()))
                .build();

        this.restClient = RestClient.builder()
                .baseUrl(BASE_URL)
                .requestFactory(new JdkClientHttpRequestFactory(httpClient))
                .defaultHeader("Authorization", "Bearer " + config.getApiKey())
                .build();
    }

    /**
     * Sends {@code prompt} to the OpenAI {@code /v1/chat/completions} endpoint
     * and returns the generated text from the first choice.
     *
     * <p>
     * The request uses a fixed system prompt that instructs the model to
     * respond only in the structured incident summary format. Temperature is
     * set to {@code 0.2} for deterministic, factual output.
     *
     * @param prompt the fully-built incident context prompt from {@link com.log0.ai_service.prompt.PromptBuilder}
     * @return the raw summary text from the model
     * @throws org.springframework.web.client.RestClientException if the API call
     *                                                            fails or returns a
     *                                                            non-2xx status
     */
    @Override
    public String generateSummary(String prompt) {
        String model = aiConfig.getOpenai().getModel();
        log.debug("Calling OpenAI API with model={}", model);

        Map<String, Object> requestBody = Map.of(
                "model", model,
                "messages", List.of(
                        Map.of("role", "system", "content", systemPrompt()),
                        Map.of("role", "user", "content", prompt)),
                "temperature", TEMPERATURE,
                "max_tokens", MAX_TOKENS);

        ChatResponse response = restClient.post()
                .uri("/v1/chat/completions")
                .body(requestBody)
                .retrieve()
                .body(ChatResponse.class);

        return response.choices().get(0).message().content();
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

    // Inner response types (OpenAI shape)

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record ChatResponse(List<Choice> choices) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record Choice(ChatMessage message) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    private record ChatMessage(String content) {
    }
}
