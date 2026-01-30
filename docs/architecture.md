# log0 Architecture Overview

## System Overview
log0 is a backend-first, multi-tenant log intelligence and incident management platform.  
It ingests high-volume logs from distributed services, normalizes and clusters similar errors, and automatically creates incidents when recurring failures are detected. The platform is designed to be scalable, reliable, and extensible for future AI-assisted debugging features.

## High-Level Architecture

The platform follows an event-driven, microservice-based architecture with clear service boundaries:

- **Ingestion Gateway**
    - Receives logs from client services via REST API
    - Authenticates requests using API keys
    - Performs rate limiting per tenant/service
    - Publishes raw logs to Kafka

- **Normalization Service**
    - Consumes raw logs from Kafka
    - Normalizes log format to a canonical schema
    - Generates a fingerprint for clustering
    - Publishes normalized logs to Kafka

- **Clustering / AI Service**
    - Consumes normalized logs
    - Groups similar logs based on fingerprint
    - Identifies recurring errors for incident creation
    - Produces incident events to Kafka

- **Incident Service**
    - Consumes incident events
    - Manages incident lifecycle (NEW → ASSIGNED → ACKED → RESOLVED)
    - Handles deduplication of incidents
    - Stores incident data in PostgreSQL

- **Notification Service**
    - Sends incident alerts to Slack and WhatsApp
    - Enables assignment and acknowledgment via interactive messages

## Data Flow
Client Services → Ingestion Gateway → Kafka (raw-logs)
→ Normalization Service → Kafka (normalized-logs)
→ Clustering / AI Service → Kafka (incident-events)
→ Incident Service → PostgreSQL
→ Notification Service → Slack/WhatsApp

## Technologies

- **Backend:** Java, Spring Boot
- **Messaging:** Apache Kafka
- **Storage:** ClickHouse (logs), PostgreSQL (incidents)
- **Notifications:** Slack, WhatsApp
- **Architecture:** Event-driven, multi-tenant microservices
