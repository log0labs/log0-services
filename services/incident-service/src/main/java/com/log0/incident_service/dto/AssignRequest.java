package com.log0.incident_service.dto;

import java.util.UUID;

import jakarta.validation.constraints.NotNull;
import lombok.Data;

@Data
public class AssignRequest {

    @NotNull
    private UUID assignedToUserId;

    @NotNull
    private UUID assignedByUserId;

    private String notes;
}
