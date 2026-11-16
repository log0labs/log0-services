package com.log0.incident_service.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.util.List;
import java.util.UUID;

import org.junit.jupiter.api.Test;

import com.log0.incident_service.async.AiSummarizer;
import com.log0.incident_service.clickhouse.LogEventRepository;
import com.log0.incident_service.dto.IncidentStatsResponse;
import com.log0.incident_service.kafka.producer.NotificationEventPublisher;
import com.log0.incident_service.repository.IncidentAssignmentRepository;
import com.log0.incident_service.repository.IncidentRepository;
import com.log0.incident_service.repository.IncidentSpecs;
import com.log0.incident_service.repository.IncidentStateHistoryRepository;
import com.log0.incident_service.statemachine.IncidentStateMachine;

class IncidentStatsTest {

    @Test
    void statsFillsMissingStatusesAndSeveritiesWithZeroAndTotalsThem() {
        IncidentRepository repo = mock(IncidentRepository.class);
        UUID tenant = UUID.randomUUID();
        when(repo.countByStatus(tenant)).thenReturn(List.<Object[]>of(
                new Object[] { "NEW", 3L }, new Object[] { "RESOLVED", 2L }));
        when(repo.countBySeverity(tenant)).thenReturn(List.<Object[]>of(new Object[] { "HIGH", 5L }));
        when(repo.findEnvironments(tenant)).thenReturn(List.of("prod", "staging"));
        IncidentService service = new IncidentService(
                repo,
                mock(IncidentAssignmentRepository.class),
                mock(IncidentStateHistoryRepository.class),
                mock(IncidentStateMachine.class),
                mock(NotificationEventPublisher.class),
                mock(AiSummarizer.class),
                mock(LogEventRepository.class));

        IncidentStatsResponse stats = service.stats(tenant);

        assertEquals(5L, stats.total());
        assertEquals(3L, stats.byStatus().get("NEW"));
        assertEquals(0L, stats.byStatus().get("ASSIGNED"));
        assertEquals(5L, stats.bySeverity().get("HIGH"));
        assertEquals(0L, stats.bySeverity().get("CRITICAL"));
        assertEquals(List.of("prod", "staging"), stats.environments());
    }

    @Test
    void likeWildcardsInUserInputMatchThemselves() {
        assertEquals("100\\%", IncidentSpecs.escapeLike("100%"));
        assertEquals("a\\_b", IncidentSpecs.escapeLike("a_b"));
        assertEquals("a\\\\b", IncidentSpecs.escapeLike("a\\b"));
    }
}
