package com.log0.incident_service.dto;

import java.util.UUID;

import jakarta.validation.constraints.NotNull;
import lombok.Data;

@Data
public class ActorRequest {

    @NotNull
    private UUID userId;
}
