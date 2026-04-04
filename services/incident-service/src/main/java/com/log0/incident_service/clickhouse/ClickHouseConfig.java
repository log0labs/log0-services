package com.log0.incident_service.clickhouse;

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
 * Registers a single {@link ClickHouseDataSource} bean used by
 * {@link LogEventRepository} to query the {@code log_events} table.
 * Connection details are bound from {@code clickhouse.*} in application.yml.
 */
@Slf4j
@Configuration
public class ClickHouseConfig {

    @Value("${clickhouse.url}")
    private String url;

    @Value("${clickhouse.username}")
    private String username;

    @Value("${clickhouse.password}")
    private String password;

    /**
     * Creates and registers a {@link ClickHouseDataSource} bean using HTTP
     * transport.
     *
     * @return a configured {@link DataSource} connected to the log0 ClickHouse
     *         database
     * @throws SQLException if the driver cannot initialise with the given URL or
     *                      properties
     */
    @Bean
    public DataSource clickHouseDataSource() throws SQLException {
        Properties props = new Properties();
        props.setProperty("user", username);
        props.setProperty("password", password);

        log.info("Initializing ClickHouse DataSource at {}", url);
        return new ClickHouseDataSource(url, props);
    }
}
