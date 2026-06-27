package com.log0.normalization_service.kafka.config;

import java.util.HashMap;
import java.util.Map;

import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.serialization.StringSerializer;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.DefaultKafkaProducerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.core.ProducerFactory;

import com.log0.normalization_service.dlq.DlqEvent;
import com.log0.normalization_service.dto.NormalizedLogEvent;

/**
 * Registers Kafka producer beans for the {@code normalized-logs} and {@code raw-logs-dlq} topics.
 *
 * Both producers are configured with {@code acks=all}, idempotence, and retries to
 * guarantee at-least-once delivery. Separate {@link ProducerFactory} instances are used
 * so each topic can carry a different value serializer without sharing state.
 */
@Configuration
public class KafkaProducerConfig {

    @Value("${spring.kafka.bootstrap-servers:localhost:9092}")
    private String bootstrapServers;

    /**
     * Builds a producer factory for {@link NormalizedLogEvent} values targeting
     * {@code normalized-logs}, with {@code linger.ms=5} to batch small bursts of events.
     */
    @Bean
    public ProducerFactory<String, NormalizedLogEvent> producerFactory() {
        Map<String, Object> config = new HashMap<>();
        config.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        config.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        config.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG,
                com.log0.normalization_service.kafka.serializer.NormalizedLogEventSerializer.class);

        // Reliability settings
        config.put(ProducerConfig.ACKS_CONFIG, "all");
        config.put(ProducerConfig.RETRIES_CONFIG, 3);
        config.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);
        config.put(ProducerConfig.MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION, 5);
        config.put(ProducerConfig.LINGER_MS_CONFIG, 5);

        return new DefaultKafkaProducerFactory<>(config);
    }

    @Bean
    public KafkaTemplate<String, NormalizedLogEvent> kafkaTemplate() {
        return new KafkaTemplate<>(producerFactory());
    }

    /**
     * Builds a producer factory for {@link DlqEvent} values targeting {@code raw-logs-dlq}.
     * No {@code linger.ms} is set here - DLQ writes are infrequent and should be flushed
     * immediately to avoid delaying failure visibility.
     */
    @Bean
    public ProducerFactory<String, DlqEvent> dlqProducerFactory() {
        Map<String, Object> config = new HashMap<>();
        config.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        config.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        config.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG,
                com.log0.normalization_service.kafka.serializer.DlqEventSerializer.class);

        // Reliability settings for DLQ
        config.put(ProducerConfig.ACKS_CONFIG, "all");
        config.put(ProducerConfig.RETRIES_CONFIG, 3);
        config.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);

        return new DefaultKafkaProducerFactory<>(config);
    }

    @Bean
    public KafkaTemplate<String, DlqEvent> dlqKafkaTemplate() {
        return new KafkaTemplate<>(dlqProducerFactory());
    }
}
