package com.log0.normalization_service.clickhouse;

import java.sql.SQLException;
import java.util.Properties;

import javax.sql.DataSource;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import com.clickhouse.jdbc.ClickHouseDataSource;

import lombok.extern.slf4j.Slf4j;

/**
 * Spring configuration for the ClickHouse {@link DataSource}.
 *
 * <p>
 * Registers a single {@link ClickHouseDataSource} bean that both
 * {@link ClickHouseInitializer} and {@link LogEventRepository} share.
 * Connection details are bound from {@code clickhouse.*} in application.yml
 * so they can be overridden per environment without code changes.
 */
@Slf4j
@Configuration
public class ClickHouseConfig {

    // Injected from clickhouse.url in application.yml
    // Format: jdbc:ch://localhost:8123/log0
    @Value("${clickhouse.url}")
    private String url;

    // Injected from clickhouse.username in application.yml
    @Value("${clickhouse.username}")
    private String username;

    // Injected from clickhouse.password in application.yml
    @Value("${clickhouse.password}")
    private String password;

    /**
     * Creates and registers a {@link ClickHouseDataSource} bean using HTTP
     * transport.
     *
     * <p>
     * The ClickHouse JDBC driver communicates over HTTP on port 8123 - this is
     * why the URL uses {@code jdbc:ch://} (ClickHouse HTTP scheme) rather than
     * a native binary protocol. HTTP is simpler to configure and sufficient for
     * the insert volumes expected in this service.
     *
     * @return a configured {@link DataSource} connected to the log0 ClickHouse
     *         database
     * @throws SQLException if the driver cannot initialise with the given URL or
     *                      properties
     */
    @Bean
    public DataSource clickHouseDataSource() throws SQLException {
        // Properties object carries credentials to the driver separately from the URL
        // so the password is not embedded in the connection string
        Properties props = new Properties();
        props.setProperty("user", username);
        props.setProperty("password", password);

        log.info("Initializing ClickHouse DataSource at {}", url);
        return new ClickHouseDataSource(url, props);
    }
}
