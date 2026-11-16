package com.log0.auth_service;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.DisabledIfEnvironmentVariable;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
@DisabledIfEnvironmentVariable(named = "CI", matches = "true", disabledReason = "Starts the whole application, which needs a live Postgres; the CI job has none")
class AuthServiceApplicationTests {

	@Test
	void contextLoads() {
	}

}
