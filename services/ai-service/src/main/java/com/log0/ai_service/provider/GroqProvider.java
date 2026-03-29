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
 * LLM provider implementation that calls the Groq API.
 *
 * <p>
 * Groq exposes an OpenAI-compatible {@code /chat/completions} endpoint,
 * so the request and response shape here is identical to {@link OpenAiProvider}
 * - only the base URL and model differ. This is the default provider because
 * Groq offers a generous free tier with extremely fast inference
 * (300+ tokens/second on Llama 3.3 70B).
 *
 * <p>
 * This bean is only created when {@code ai.provider=groq} in
 * {@code application.yml}. Switching providers requires no code changes -
 * only a config change.
 */
@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "ai.provider", havingValue = "groq")
public class GroqProvider implements LlmProvider {
    private static final String BASE_URL = "https://api.groq.com";
    private static final double TEMPERATURE = 0.2;
    private static final int MAX_TOKENS = 300;

    private final AiConfig aiConfig;

    private RestClient restClient;

    /**
     * Initialises the {@link RestClient} after Spring has injected
     * {@link AiConfig}. Sets the base URL, Authorization header, and
     * connect timeout from the {@code ai.groq} config block.
     */
    @PostConstruct
    private void init() {
        AiConfig.ProviderConfig config = aiConfig.getGroq();

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
     * Sends {@code prompt} to the Groq {@code /chat/completions} endpoint and
     * returns the generated text from the first choice.
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
        String model = aiConfig.getGroq().getModel();
        log.debug("Calling Groq API with model={}", model);

        Map<String, Object> requestBody = Map.of(
                "model", model,
                "messages", List.of(
                        Map.of("role", "system", "content", systemPrompt()),
                        Map.of("role", "user", "content", prompt)),
                "temperature", TEMPERATURE,
                "max_tokens", MAX_TOKENS);

        ChatResponse response = restClient.post()
                .uri("/openai/v1/chat/completions")
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

    // Inner response types (Groq / OpenAI-compatible shape)

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
