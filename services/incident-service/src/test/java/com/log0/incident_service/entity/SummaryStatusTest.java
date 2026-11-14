package com.log0.incident_service.entity;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.time.Instant;

import org.junit.jupiter.api.Test;

class SummaryStatusTest {

    private final Instant now = Instant.parse("2026-11-15T10:00:00Z");

    @Test
    void readyWhenThereIsText() {
        Incident i = new Incident();
        i.setAiSummary("The pool is exhausted.");
        assertEquals("READY", i.summaryStatus(now));
    }

    @Test
    void pendingWhileARecentRequestIsInFlight() {
        Incident i = new Incident();
        i.setAiSummaryRequestedAt(now.minusSeconds(60));
        assertEquals("PENDING", i.summaryStatus(now));
    }

    @Test
    void unavailableOnceTheWindowPassesWithNoText() {
        Incident i = new Incident();
        i.setAiSummaryRequestedAt(now.minusSeconds(60 * 4));
        assertEquals("UNAVAILABLE", i.summaryStatus(now));
    }

    @Test
    void unavailableForOldRowsThatNeverRecordedARequest() {
        assertEquals("UNAVAILABLE", new Incident().summaryStatus(now));
    }

    @Test
    void blankTextIsNotASummary() {
        Incident i = new Incident();
        i.setAiSummary("   ");
        assertEquals("UNAVAILABLE", i.summaryStatus(now));
    }
}
