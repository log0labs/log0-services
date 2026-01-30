# log0 Development Roadmap

## MVP Features (Phase 1)
1. **Log Ingestion**
    - Ingestion Gateway service with API key authentication
    - Rate limiting per tenant/service
    - Kafka integration for raw log streaming

2. **Log Normalization**
    - Normalization service converting logs to canonical schema
    - Fingerprint generation for clustering

3. **Error Clustering**
    - Group similar logs
    - Trigger incidents for recurring errors

4. **Incident Management**
    - Incident lifecycle management (NEW → ASSIGNED → ACKED → RESOLVED)
    - Deduplication
    - PostgreSQL storage

5. **Notification System**
    - Slack alerting for assigned admins
    - Incident assignment from Slack

---

## Phase 2 (Future Enhancements)
- WhatsApp notification support
- AI-assisted incident summaries
- Integration with private code repositories for error context
- Advanced log querying and visualization (possible frontend dashboards)
- User role management and RBAC

---

## Phase 3 (Long-Term Goals)
- Frontend platform/dashboard
- Self-hosted deployment support
- AI-based code suggestions for fixing recurring errors
- Analytics and alerting dashboards for operational insights
