package com.log0.notification_service.service;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Map;

import org.springframework.stereotype.Service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.log0.notification_service.config.SlackConfig;
import com.log0.notification_service.dto.NotificationEvent;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

@Slf4j
@Service
@RequiredArgsConstructor
public class SlackNotifier {
    private static final String SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage";

    private final SlackConfig slackConfig;
    private final HttpClient httpClient = HttpClient.newHttpClient();
    private final ObjectMapper objectMapper = new ObjectMapper().registerModule(new JavaTimeModule());

    public void notify(NotificationEvent event) {
        String title = buildTitle(event);
        String body = buildBody(event);

        Map<String, Object> payload = Map.of(
                "channel", slackConfig.getChannelId(),
                "blocks", buildBlocks(title, body, event));

        try {
            String json = objectMapper.writeValueAsString(payload);
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(SLACK_POST_MESSAGE_URL))
                    .header("Content-Type", "application/json")
                    .header("Authorization", "Bearer " + slackConfig.getBotToken())
                    .POST(HttpRequest.BodyPublishers.ofString(json))
                    .build();

            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            log.info("Slack response: {}", response.body());
        } catch (IOException | InterruptedException e) {
            throw new RuntimeException("Failed to send Slack notification", e);
        }
    }

    private String buildTitle(NotificationEvent event) {
        return switch (event.getNotificationType()) {
            case "INCIDENT_CREATED" -> ":rotating_light: New Incident — " + event.getServiceName();
            case "INCIDENT_ASSIGNED" -> ":bust_in_silhouette: Incident Assigned — " + event.getServiceName();
            case "INCIDENT_RESOLVED" -> ":white_check_mark: Incident Resolved — " + event.getServiceName();
            default -> "Incident Update — " + event.getServiceName();
        };
    }

    private String buildBody(NotificationEvent event) {
        StringBuilder sb = new StringBuilder();
        sb.append("*Severity:* ").append(event.getSeverity()).append("\n");
        sb.append("*Environment:* ").append(event.getEnvironment()).append("\n");
        sb.append("*Occurrences:* ").append(event.getOccurrenceCount()).append("\n");
        sb.append("*Status:* ").append(event.getStatus()).append("\n");
        if (event.getAssignedToUserId() != null) {
            sb.append("*Assigned To:* ").append(event.getAssignedToUserId()).append("\n");
        }
        if (event.getAiSummary() != null) {
            sb.append("*AI Summary:* ").append(event.getAiSummary()).append("\n");
        }
        return sb.toString();
    }

    private Object buildBlocks(String title, String body, NotificationEvent event) {
        return java.util.List.of(
                Map.of(
                        "type", "header",
                        "text", Map.of("type", "plain_text", "text", title)),
                Map.of(
                        "type", "section",
                        "text", Map.of("type", "mrkdwn", "text", body)),
                Map.of(
                        "type", "context",
                        "elements", java.util.List.of(
                                Map.of("type", "mrkdwn", "text", "*Incident ID:* " + event.getIncidentId()))));
    }
}
