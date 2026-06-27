# Generic multi-stage build for ANY log0 Spring Boot service.
# Build context = a single service directory (has pom.xml + mvnw + src).
# One Dockerfile, reused by all 7 services via compose `build.dockerfile`.
#
# Java 25, Spring Boot 3, Maven (via the project's own mvnw wrapper).

# ---- build stage ----
FROM eclipse-temurin:25-jdk AS build
WORKDIR /app
COPY . .
# Windows checkouts ship mvnw with CRLF -> normalize + make executable.
RUN sed -i 's/\r$//' mvnw && chmod +x mvnw
# Shared, cached Maven repo across all service builds (BuildKit cache mount).
RUN --mount=type=cache,target=/root/.m2 ./mvnw -B -q -DskipTests clean package

# ---- runtime stage ----
FROM eclipse-temurin:25-jre
WORKDIR /app
# Spring Boot fat jar. *.jar.original is skipped (different extension).
COPY --from=build /app/target/*.jar app.jar
# Heap auto-sizes to a % of the container's mem_limit (set in compose).
ENV JAVA_OPTS="-XX:MaxRAMPercentage=70.0 -XX:+UseSerialGC"
EXPOSE 8080
ENTRYPOINT ["sh","-c","exec java $JAVA_OPTS -jar app.jar"]
