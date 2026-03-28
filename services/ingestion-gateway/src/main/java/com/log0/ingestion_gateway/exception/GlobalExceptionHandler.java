package com.log0.ingestion_gateway.exception;

import java.util.List;
import java.util.stream.Collectors;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.resource.NoResourceFoundException;

import lombok.extern.slf4j.Slf4j;

/**
 * Centralises exception-to-HTTP-response mapping for all controllers in the
 * ingestion gateway, ensuring every error surface returns a consistent
 * {@link ErrorResponse} body rather than Spring's default error format.
 */
@Slf4j
@RestControllerAdvice
public class GlobalExceptionHandler {

        /**
         * Collects all field-level constraint violations from a failed {@code @Valid}
         * binding and returns them as a structured 400 response.
         */
        @ExceptionHandler(MethodArgumentNotValidException.class)
        public ResponseEntity<ErrorResponse> handleValidationException(
                        MethodArgumentNotValidException ex) {
                List<String> errors = ex.getBindingResult()
                                .getFieldErrors()
                                .stream()
                                .map(error -> error.getField() + ": " + error.getDefaultMessage())
                                .collect(Collectors.toList());

                ErrorResponse response = new ErrorResponse(
                                "Validation Failed",
                                errors);

                return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(response);
        }

        /**
         * Maps {@link IllegalArgumentException} - thrown for missing required headers or
         * other caller-supplied bad values - to a 400 Bad Request response.
         */
        @ExceptionHandler(IllegalArgumentException.class)
        public ResponseEntity<ErrorResponse> handleIllegalArgumentException(
                        IllegalArgumentException ex) {
                ErrorResponse response = new ErrorResponse(
                                "Invalid Argument",
                                List.of(ex.getMessage()));

                return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(response);
        }

        @ExceptionHandler(NoResourceFoundException.class)
        public ResponseEntity<ErrorResponse> handleNoResourceFoundException(NoResourceFoundException ex) {
                ErrorResponse response = new ErrorResponse(
                                "Not Found",
                                List.of(ex.getMessage()));

                return ResponseEntity.status(HttpStatus.NOT_FOUND).body(response);
        }

        /**
         * Catch-all for any unhandled exception; logs the full stack trace and returns
         * a 500 response without leaking internal details to the caller.
         */
        @ExceptionHandler(Exception.class)
        public ResponseEntity<ErrorResponse> handleGenericException(Exception ex) {
                log.error("Unhandled exception while processing request", ex);
                ErrorResponse response = new ErrorResponse(
                                "Internal Server Error",
                                List.of("An unexpected error occurred."));

                return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(response);
        }
}
