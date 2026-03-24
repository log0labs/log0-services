package com.log0.normalization_service.processor;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.regex.Pattern;

import org.springframework.stereotype.Component;

/**
 * Generates deterministic fingerprints for log events.
 *
 * A fingerprint is a SHA-256 hash that identifies the structural pattern of an error,
 * independent of its dynamic values (timestamps, IDs, counts). Two log messages that differ
 * only in variable data — different user IDs, different timeout durations, different IP
 * addresses — will produce the same fingerprint and therefore collapse into the same incident.
 *
 * This is the foundation of log0's deduplication. Without stable fingerprints, the
 * Clustering Service cannot group related errors, and 10,000 identical exceptions would
 * create 10,000 separate incidents instead of one.
 */
@Component
public class FingerprintGenerator {

    /**
     * Matches standard UUID format (8-4-4-4-12 hex chars), case-insensitive.
     * Must be replaced before IP_PATTERN and NUMBER_PATTERN because UUIDs contain
     * hex digits and hyphens that would be partially consumed by number matching.
     *
     * Example: {@code a3f4b2c1-1234-5678-abcd-ef0123456789} → {@code <uuid>}
     */
    private static final Pattern UUID_PATTERN = Pattern
            .compile("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", Pattern.CASE_INSENSITIVE);

    /**
     * Matches IPv4 addresses (e.g. 192.168.1.1).
     * Must be replaced before NUMBER_PATTERN because an IP address is entirely composed
     * of digits and dots — NUMBER_PATTERN would consume the digit groups first, turning
     * {@code 192.168.1.1} into {@code <number>.<number>.<number>.<number>} before
     * IP_PATTERN ever gets a chance to match.
     *
     * Example: {@code 192.168.1.1} → {@code <ip>}
     */
    private static final Pattern IP_PATTERN = Pattern.compile("\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}");

    /**
     * Matches any sequence of digits. Applied last because it is the most general pattern
     * and would interfere with UUID and IP matching if applied first.
     *
     * Example: {@code timeout after 30000ms} → {@code timeout after <number>ms}
     */
    private static final Pattern NUMBER_PATTERN = Pattern.compile("\\d+");

    /**
     * Strips all dynamic values from a log message and returns a stable structural template.
     *
     * The template is what makes deduplication possible. Consider three log lines emitted
     * by the same bug:
     * <pre>
     *   "Connection timeout to payment-gateway after 30000ms"
     *   "Connection timeout to payment-gateway after 28514ms"
     *   "Connection timeout to payment-gateway after 31003ms"
     * </pre>
     * All three produce the same template "Connection timeout to payment-gateway after
     * <number>ms", which leads to the same fingerprint and collapses all three into
     * one incident.
     *
     * Replacement order is critical — most specific patterns must run first:
     * - UUID: contains hex digit groups that NUMBER_PATTERN would partially consume
     * - IP:   contains digit groups that NUMBER_PATTERN would consume
     * - Number: catches all remaining digit sequences
     *
     * Full example:
     * <pre>
     *   Input:  "Timeout from 192.168.1.1 after 30000ms, id=a3f4b2c1-1234-5678-abcd-ef0123456789"
     *   →UUID:  "Timeout from 192.168.1.1 after 30000ms, id=<uuid>"
     *   →IP:    "Timeout from <ip> after 30000ms, id=<uuid>"
     *   →Num:   "Timeout from <ip> after <number>ms, id=<uuid>"
     * </pre>
     *
     * @param message the normalized log message (should already be trimmed)
     * @return a stable structural template with dynamic values replaced by placeholders;
     *         empty string if message is null or empty
     */
    public String buildMessageTemplate(String message) {
        if (message == null || message.isEmpty()) {
            return "";
        }

        String template = UUID_PATTERN.matcher(message).replaceAll("<uuid>");
        template = IP_PATTERN.matcher(template).replaceAll("<ip>");
        template = NUMBER_PATTERN.matcher(template).replaceAll("<number>");

        return template.trim();
    }

    /**
     * Extracts the method reference from the first line of a stack trace, with the
     * source file and line number removed.
     *
     * Only the first frame is used because it represents the exact call site that threw
     * the exception — the root of the problem. Deeper frames (callers) vary across different
     * request paths but the throw site is stable for the same bug.
     *
     * Line numbers are deliberately stripped. A refactor that shifts
     * {@code PaymentProcessor.charge} from line 142 to line 145 does not change the bug —
     * it must still produce the same fingerprint and map to the same existing incident.
     *
     * Example:
     * <pre>
     *   Input:         "at com.log0.PaymentProcessor.charge(PaymentProcessor.java:142)"
     *   strip "at ":   "com.log0.PaymentProcessor.charge(PaymentProcessor.java:142)"
     *   strip parens:  "com.log0.PaymentProcessor.charge"
     * </pre>
     *
     * @param stackTrace the raw stack trace string from the log event (may be multi-line)
     * @return the class and method name of the first frame, without file name or line number;
     *         empty string if stackTrace is null or empty
     */
    private String extractFirstFrame(String stackTrace) {
        if (stackTrace == null || stackTrace.isEmpty()) {
            return "";
        }

        String firstLine = stackTrace.split("\n")[0].trim();
        firstLine = firstLine.replaceFirst("^at\\s+", "");  // remove "at " prefix if present
        firstLine = firstLine.replaceAll("\\(.*?\\)", "");  // remove (FileName.java:142)

        return firstLine.trim();
    }

    /**
     * Returns the input string unchanged, or an empty string if it is null.
     *
     * Guards against null values before string concatenation in {@link #generate}.
     * Without this, a null service name would produce the literal text {@code "null"} in the
     * hash input — {@code "null|template|..."} — which is semantically wrong and would create
     * a different fingerprint than the intended {@code "|template|..."}.
     *
     * @param s any string, possibly null
     * @return {@code s} if non-null, {@code ""} otherwise
     */
    private String safe(String s) {
        return s == null ? "" : s;
    }

    /**
     * Computes a SHA-256 hash of the input string and returns it as a 64-character
     * lowercase hex string.
     *
     * SHA-256 is used rather than MD5 or CRC32 because:
     * - Collision-resistant: two structurally different error patterns will not accidentally
     *   hash to the same fingerprint
     * - Available on all Java SE compliant JVMs without additional dependencies
     * - Deterministic: given the same input string and charset, always produces the same
     *   64-char output regardless of platform or JVM version
     *
     * The {@code 0xff & b} mask is required because Java's {@code byte} is signed (-128 to 127).
     * Without the mask, {@code Integer.toHexString} on a negative byte would produce a
     * sign-extended 8-digit value (e.g. {@code ffffff80}) instead of the correct 2-digit
     * value ({@code 80}).
     *
     * The leading-zero pad ({@code if (h.length() == 1) hex.append('0')}) ensures each byte
     * is always represented as exactly 2 hex digits, keeping the output length fixed at 64.
     *
     * @param input the string to hash; must not be null
     * @return 64-character lowercase hex string representing the SHA-256 digest
     * @throws RuntimeException if SHA-256 is unavailable (cannot happen on any standard JVM)
     */
    private String sha256(String input) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hashBytes = digest.digest(input.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder();
            for (byte b : hashBytes) {
                String h = Integer.toHexString(0xff & b);
                if (h.length() == 1)
                    hex.append('0');
                hex.append(h);
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new RuntimeException("SHA-256 algorithm not found", e);
        }
    }

    /**
     * Generates a deterministic fingerprint for a log event from its four stable structural
     * components.
     *
     * Fingerprint formula:
     * <pre>
     *   SHA-256( service | messageTemplate | exceptionType | firstStackFrame )
     * </pre>
     *
     * Each component contributes a distinct dimension of specificity:
     * - {@code service}: scopes the fingerprint to the originating service. A
     *   {@code NullPointerException} in {@code payment-service} is a different incident
     *   from the same exception in {@code auth-service}.
     * - {@code template}: structurally stable form of the error message with dynamic values
     *   replaced by placeholders (see {@link #buildMessageTemplate}).
     * - {@code exceptionType}: distinguishes different exception classes at the same call site.
     *   A {@code TimeoutException} and {@code NullPointerException} both thrown from
     *   {@code PaymentProcessor.charge} are separate bugs.
     * - {@code stackTrace}: first frame without line numbers, pinning the fingerprint to the
     *   exact throw site (see {@link #extractFirstFrame}).
     *
     * The {@code |} pipe delimiter prevents hash collisions from adjacent field boundaries.
     * Without it, {@code ("ab", "c")} and {@code ("a", "bc")} would produce the same input
     * {@code "abc"} and therefore the same fingerprint despite being different error patterns.
     *
     * All inputs are passed through {@link #safe} — null fields produce empty strings, not
     * the literal text {@code "null"}.
     *
     * Example — two log lines from the same bug produce the same fingerprint:
     * <pre>
     *   generate("payment-service", "Connection timeout after <number>ms", null,
     *            "at com.log0.PaymentProcessor.charge(PaymentProcessor.java:142)")
     *
     *   generate("payment-service", "Connection timeout after <number>ms", null,
     *            "at com.log0.PaymentProcessor.charge(PaymentProcessor.java:145)")
     *
     *   Both produce: "a3f8c2..." (identical fingerprint → same incident)
     * </pre>
     *
     * @param service       the originating service name (e.g. {@code "payment-service"})
     * @param template      the message template from {@link #buildMessageTemplate}
     * @param exceptionType the exception class name if known (e.g. {@code "NullPointerException"}),
     *                      or null if not available in the current schema
     * @param stackTrace    the raw stack trace string from the log event, or null if absent
     * @return a 64-character lowercase hex SHA-256 fingerprint, always non-null
     */
    public String generate(String service, String template, String exceptionType, String stackTrace) {
        String firstFrame = extractFirstFrame(stackTrace);
        String input = safe(service) + "|" + safe(template) + "|" + safe(exceptionType) + "|" + safe(firstFrame);
        return sha256(input);
    }
}
