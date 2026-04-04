package com.log0.incident_service.clickhouse;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;

import javax.sql.DataSource;

/**
 * Explicit PostgreSQL {@link DataSource} configuration.
 *
 * <p>
 * Spring Boot's {@code DataSourceAutoConfiguration} backs off when any
 * {@code DataSource} bean is present in the context. Because
 * {@link ClickHouseConfig} registers a second {@code DataSource} bean for
 * ClickHouse, we must declare the PostgreSQL DataSource explicitly here and
 * mark it {@code @Primary} so that JPA and Hibernate target PostgreSQL,
 * not ClickHouse.
 */
@Configuration
public class PostgresDataSourceConfig {

    @Value("${spring.datasource.url}")
    private String url;

    @Value("${spring.datasource.username}")
    private String username;

    @Value("${spring.datasource.password}")
    private String password;

    /**
     * Builds the primary PostgreSQL {@link DataSource} using HikariCP.
     * Reads {@code spring.datasource.*} directly via {@code @Value} because
     * {@code DataSourceBuilder} + {@code @ConfigurationProperties} does not
     * reliably map {@code spring.datasource.url} to HikariCP's {@code jdbcUrl}
     * when a second {@code DataSource} bean is present in the context.
     *
     * @return a HikariCP DataSource pointed at PostgreSQL
     */
    @Bean
    @Primary
    public DataSource dataSource() {
        HikariConfig config = new HikariConfig();
        config.setJdbcUrl(url);
        config.setUsername(username);
        config.setPassword(password);
        return new HikariDataSource(config);
    }
}
