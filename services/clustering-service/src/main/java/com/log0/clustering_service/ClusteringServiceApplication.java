package com.log0.clustering_service;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationProperties;

@SpringBootApplication
@ConfigurationProperties
public class ClusteringServiceApplication {

	public static void main(String[] args) {
		SpringApplication.run(ClusteringServiceApplication.class, args);
	}

}
