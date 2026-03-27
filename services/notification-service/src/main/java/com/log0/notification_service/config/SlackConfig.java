package com.log0.notification_service.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import lombok.Getter;
import lombok.Setter;

@Getter
@Setter
@ConfigurationProperties(prefix = "slack")
public class SlackConfig {
    private String botToken;
    private String channelId;
}
