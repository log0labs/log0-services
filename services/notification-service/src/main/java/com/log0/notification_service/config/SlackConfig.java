package com.log0.notification_service.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import lombok.Getter;
import lombok.Setter;

/**
 * Binds {@code slack.*} properties from application configuration to a typed bean.
 * Provides the bot token and target channel ID consumed by {@link com.log0.notification_service.service.SlackNotifier}.
 */
@Getter
@Setter
@ConfigurationProperties(prefix = "slack")
public class SlackConfig {
    private String botToken;
    private String channelId;
}
