package com.log0.normalization_service.clickhouse;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Statement;

import javax.sql.DataSource;

import org.springframework.boot.CommandLineRunner;
import org.springframework.core.io.ClassPathResource;
import org.springframework.stereotype.Component;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

/**
 * Runs ClickHouse DDL migrations on application startup.
 *
 * <p>
 * Reads SQL files from {@code classpath:clickhouse/} and executes them in
 * order.
 * All statements use {@code CREATE TABLE IF NOT EXISTS} so they are safe to
 * re-run
 * on every startup - equivalent to Flyway but without the transaction
 * requirements
 * that ClickHouse does not support.
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class ClickHouseInitializer implements CommandLineRunner {
    private final DataSource clickHouseDataSource;

    /**
     * Executes all ClickHouse schema migration files on startup.
     *
     * @param args application arguments (unused)
     */
    @Override
    public void run(String... args) {
        log.info("Running ClickHouse schema initialization...");
        executeSqlFile("clickhouse/V1__create_log_events.sql");
        log.info("ClickHouse schema initialization complete.");
    }

    /**
     * Reads a SQL file from the classpath and executes it against ClickHouse.
     *
     * <p>
     * The file is read as UTF-8 text and executed as a single statement.
     * If either the file cannot be read or the SQL fails to execute, a
     * {@link RuntimeException} is thrown to fail the application startup fast -
     * running without the required schema would cause silent insert failures.
     *
     * @param path classpath-relative path to the SQL file (e.g.
     *             {@code "clickhouse/V1__create_log_events.sql"})
     * @throws RuntimeException if the file cannot be read or the SQL execution
     *                          fails
     */
    private void executeSqlFile(String path) {
        try {
            ClassPathResource resource = new ClassPathResource(path);
            String sql = resource.getContentAsString(StandardCharsets.UTF_8);

            try (Connection conn = clickHouseDataSource.getConnection(); Statement stmt = conn.createStatement()) {
                stmt.execute(sql);
                log.info("Executed: {}", path);
            }
        } catch (SQLException | IOException e) {
            log.error("Failed to execute ClickHouse migration {}: {}", path, e.getMessage(), e);
            throw new RuntimeException("ClickHouse schema initialization failed", e);
        }
    }
}
