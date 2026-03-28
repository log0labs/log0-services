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

/**
 * Delivers incident notifications to a configured Slack channel via the
 * Slack Web API ({@code chat.postMessage}). Message structure - header, body, and
 * context footer - is derived from the {@link NotificationEvent} notification type,
 * so callers only need to invoke {@link #notify(NotificationEvent)}.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class SlackNotifier {
    private static final String SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage";

    private final SlackConfig slackConfig;
    private final HttpClient httpClient = HttpClient.newHttpClient();
    private final ObjectMapper objectMapper = new ObjectMapper().registerModule(new JavaTimeModule());

    /**
     * Formats and POSTs a Slack Block Kit message for the given incident event.
     * Callers should treat any thrown exception as a signal to route the event to the DLQ.
     *
     * @param event the notification event to deliver
     * @throws RuntimeException wrapping {@link IOException} or {@link InterruptedException}
     *                          if the HTTP call to the Slack API fails
     */
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

    /**
     * Returns an emoji-prefixed header string that reflects the notification type.
     * Falls back to a generic "Incident Update" title for unrecognised types.
     */
    private String buildTitle(NotificationEvent event) {
        return switch (event.getNotificationType()) {
            case "INCIDENT_CREATED" -> ":rotating_light: New Incident - " + event.getServiceName();
            case "INCIDENT_ASSIGNED" -> ":bust_in_silhouette: Incident Assigned - " + event.getServiceName();
            case "INCIDENT_RESOLVED" -> ":white_check_mark: Incident Resolved - " + event.getServiceName();
            default -> "Incident Update - " + event.getServiceName();
        };
    }

    /**
     * Assembles the markdown body section from the event's structured fields,
     * omitting optional fields ({@code assignedToUserId}, {@code aiSummary}) when absent.
     */
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

    /**
     * Constructs the Slack Block Kit block list: a header block, a section block for the body,
     * and a context block displaying the incident ID.
     */
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
