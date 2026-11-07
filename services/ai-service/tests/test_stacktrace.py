"""Tests for app.parsing.stacktrace (pure functions, no I/O)."""

import pytest

from app.parsing.stacktrace import MAX_FRAMES, parse_stack_trace

# ---- samples copied from log0's real data and from common runtimes -----------------

JAVA_BARE = "com.log0.PaymentProcessor.charge(PaymentProcessor.java:42)"

JAVA_FULL = (
    "java.net.SocketTimeoutException: Connection timeout after 30000ms calling payment gateway\n"
    "\tat com.log0.payment.PaymentProcessor.charge(PaymentProcessor.java:120)\n"
    "\tat com.log0.common.Dispatcher.run(Dispatcher.java:88)\n"
)

JAVA_CHAINED = """\
org.springframework.web.client.ResourceAccessException: I/O error on POST request
\tat org.springframework.web.client.RestTemplate.doExecute(RestTemplate.java:785)
\tat com.log0.payment.PaymentProcessor.lambda$charge$0(PaymentProcessor.java:64)
\tat com.log0.payment.PaymentProcessor$Retry.run(PaymentProcessor.java:90)
\tat java.base/java.lang.Thread.run(Thread.java:833)
\tat java.base/jdk.internal.misc.Unsafe.park(Native Method)
Caused by: java.net.SocketTimeoutException: Read timed out
\tat java.base/java.net.SocketInputStream.read(SocketInputStream.java:186)
\t... 12 more
"""

PYTHON = """\
Traceback (most recent call last):
  File "/app/payments/gateway.py", line 142, in charge
    resp = client.post(url)
  File "/usr/lib/python3.11/site-packages/requests/api.py", line 59, in post
    return request("post", url)
requests.exceptions.ConnectionError: Max retries exceeded
"""

NODE = """\
TypeError: Cannot read properties of undefined (reading 'id')
    at charge (/app/src/gateway.js:142:15)
    at async Server.handle (/app/src/server.js:20:7)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
    at node:internal/process/task_queues:95:5
"""

GO = """\
panic: runtime error: index out of range [5] with length 3

goroutine 1 [running]:
main.(*Server).charge(0xc000010000, 0x5)
\t/app/gateway.go:142 +0x1d
main.main()
\t/app/main.go:20 +0x25
runtime.main()
\t/usr/local/go/src/runtime/proc.go:250 +0x207
"""


# ------------------------------------------------------------------------- Java


def test_java_bare_frame_is_what_most_real_log_lines_look_like() -> None:
    out = parse_stack_trace(JAVA_BARE)
    assert out.language == "java"
    assert out.exception_type is None  # a bare frame has no exception header
    (frame,) = out.frames
    assert (frame.module, frame.function, frame.file, frame.line) == (
        "com.log0.PaymentProcessor", "charge", "PaymentProcessor.java", 42,
    )
    assert frame.path_hint == "com/log0/PaymentProcessor.java"
    assert frame.in_app is True


def test_java_full_trace_has_exception_and_ordered_frames() -> None:
    out = parse_stack_trace(JAVA_FULL)
    assert out.exception_type == "java.net.SocketTimeoutException"
    assert out.exception_message == "Connection timeout after 30000ms calling payment gateway"
    assert [(f.function, f.line) for f in out.frames] == [("charge", 120), ("run", 88)]
    assert out.first_in_app_frame() == out.frames[0]


def test_java_chained_trace_classifies_library_frames_and_causes() -> None:
    out = parse_stack_trace(JAVA_CHAINED)
    by_function = {f.function: f for f in out.frames}
    assert by_function["doExecute"].in_app is False  # Spring
    assert out.caused_by == ["java.net.SocketTimeoutException: Read timed out"]
    first = out.first_in_app_frame()
    assert first is not None and first.function == "lambda$charge$0"  # skips the Spring frame


def test_java_inner_class_and_module_prefix_and_native_method() -> None:
    out = parse_stack_trace(JAVA_CHAINED)
    inner = next(f for f in out.frames if f.module == "com.log0.payment.PaymentProcessor$Retry")
    assert inner.path_hint == "com/log0/payment/PaymentProcessor.java"  # $Inner stripped
    thread = next(f for f in out.frames if f.module == "java.lang.Thread")
    assert thread.in_app is False  # "java.base/" prefix removed before classifying
    native = next(f for f in out.frames if f.file == "Native Method")
    assert native.line is None and native.path_hint is None


def test_kotlin_file_extension_is_kept_in_path_hint() -> None:
    out = parse_stack_trace("at com.log0.billing.InvoiceService.create(InvoiceService.kt:31)")
    assert out.frames[0].path_hint == "com/log0/billing/InvoiceService.kt"


# ----------------------------------------------------------------------- Python


def test_python_frames_are_reversed_to_innermost_first() -> None:
    out = parse_stack_trace(PYTHON)
    assert out.language == "python"
    assert out.exception_type == "requests.exceptions.ConnectionError"
    assert out.exception_message == "Max retries exceeded"
    assert [f.function for f in out.frames] == ["post", "charge"]
    assert out.frames[0].in_app is False  # site-packages
    first = out.first_in_app_frame()
    assert first is not None
    assert (first.file, first.line, first.function) == ("/app/payments/gateway.py", 142, "charge")


# ------------------------------------------------------------------------- Node


def test_node_frames_with_and_without_function_names() -> None:
    out = parse_stack_trace(NODE)
    assert out.language == "node"
    assert out.exception_type == "TypeError"
    assert out.exception_message == "Cannot read properties of undefined (reading 'id')"
    functions = [f.function for f in out.frames]
    assert functions == ["charge", "Server.handle", "Layer.handle [as handle_request]", "<anonymous>"]
    first = out.frames[0]
    assert (first.file, first.line, first.column, first.in_app) == ("/app/src/gateway.js", 142, 15, True)
    assert out.frames[2].in_app is False  # node_modules
    assert out.frames[3].in_app is False  # node:internal


# --------------------------------------------------------------------------- Go


def test_go_panic_frames() -> None:
    out = parse_stack_trace(GO)
    assert out.language == "go"
    assert out.exception_type == "panic"
    assert out.exception_message == "runtime error: index out of range [5] with length 3"
    first = out.frames[0]
    assert (first.module, first.function, first.file, first.line) == ("main", "charge", "/app/gateway.go", 142)
    assert out.frames[-1].in_app is False  # Go standard library
    assert out.first_in_app_frame() == first


# --------------------------------------------------------------- robustness / caps


@pytest.mark.parametrize("text", [None, "", "   \n  ", "just a log line, nothing to parse", "}{ ::: ((("])
def test_garbage_never_raises_and_has_no_frames(text: str | None) -> None:
    out = parse_stack_trace(text)
    assert out.language is None and out.frames == [] and out.first_in_app_frame() is None


def test_newlines_stored_as_two_characters_are_still_parsed() -> None:
    escaped = JAVA_FULL.replace("\n", "\\n").replace("\t", "\\t")
    assert parse_stack_trace(escaped) == parse_stack_trace(JAVA_FULL)


def test_windows_line_endings() -> None:
    assert parse_stack_trace(JAVA_FULL.replace("\n", "\r\n")) == parse_stack_trace(JAVA_FULL)


def test_frames_are_capped_and_flagged() -> None:
    many = "\n".join(f"\tat com.log0.A.m{i}(A.java:{i + 1})" for i in range(MAX_FRAMES + 20))
    out = parse_stack_trace("java.lang.RuntimeError: deep\n" + many)
    assert len(out.frames) == MAX_FRAMES and out.truncated is True


def test_long_exception_message_is_cut() -> None:
    out = parse_stack_trace("java.lang.RuntimeException: " + "x" * 5000 + "\n\tat com.log0.A.m(A.java:1)")
    assert out.exception_message is not None and len(out.exception_message) == 500


def test_all_in_library_code_has_no_first_in_app_frame() -> None:
    out = parse_stack_trace("\tat java.base/java.lang.Thread.run(Thread.java:833)")
    assert out.frames and out.first_in_app_frame() is None
