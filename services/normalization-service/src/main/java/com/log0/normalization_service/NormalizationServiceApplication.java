package com.log0.normalization_service;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
public class NormalizationServiceApplication {

	public static void main(String[] args) {
		SpringApplication.run(NormalizationServiceApplication.class, args);
	}

}
