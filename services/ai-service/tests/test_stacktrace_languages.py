"""Stack-trace parsing for the other languages (Rust, C#, Ruby, PHP, Swift, Dart, Perl, R,
MATLAB, shell, C/C++, Scala, JS in browsers). Core formats live in test_stacktrace.py."""

from app.parsing.stacktrace import parse_stack_trace

SCALA = """\
java.lang.IllegalStateException: boom
\tat com.log0.billing.Invoices$.create(Invoices.scala:31)
\tat com.log0.billing.Invoices.$anonfun$run$1(Invoices.scala:20)
\tat scala.collection.immutable.List.foreach(List.scala:333)
"""

CSHARP = """\
System.NullReferenceException: Object reference not set to an instance of an object.
   at MyApp.Payments.Gateway.Charge(Int32 amount) in /app/Payments/Gateway.cs:line 142
   at MyApp.Program.Main(String[] args) in /app/Program.cs:line 20
   at System.Threading.Tasks.Task.Execute()
"""

RUST = """\
thread 'main' panicked at src/gateway.rs:142:9:
index out of bounds: the len is 3 but the index is 5
stack backtrace:
   0: rust_begin_unwind
             at /rustc/90b35a6239c3d8bdabc530a6a0816f7ff89a0aaf/library/std/src/panicking.rs:645:5
   1: core::panicking::panic_fmt
             at /rustc/90b35a6239c3d8bdabc530a6a0816f7ff89a0aaf/library/core/src/panicking.rs:72:14
   2: myapp::gateway::charge::h1a2b3c4d5e6f7081
             at ./src/gateway.rs:142:9
   3: myapp::main
             at ./src/main.rs:20:5
"""

RUBY = """\
/app/gateway.rb:142:in 'Gateway#charge': boom (RuntimeError)
\tfrom /app/server.rb:20:in 'block in <main>'
\tfrom /usr/local/bundle/gems/rack-3.0/lib/rack/handler.rb:10:in 'call'
"""

PHP = """\
PHP Fatal error:  Uncaught Exception: boom in /app/Gateway.php:12
Stack trace:
#0 /app/Controller.php(30): Gateway->charge(100)
#1 /app/vendor/slim/slim/App.php(55): Controller->handle(Object(Request))
#2 /app/index.php(10): Slim\\App->run()
#3 {main}
  thrown in /app/Gateway.php on line 12
"""

SWIFT = """\
Fatal error: Index out of range: file /app/Sources/App/Gateway.swift, line 142
Current stack trace:
0    libswiftCore.dylib            0x00007ff8 _assertionFailure(_:_:file:line:flags:) + 481
1    MyApp                         0x0000000100003f20 Gateway.charge(amount: Int) + 52 (Gateway.swift:142)
2    MyApp                         0x0000000100003f90 main + 20 (main.swift:20)
"""

DART = """\
Unhandled exception:
Bad state: Insufficient funds
#0      Gateway.charge (package:myapp/gateway.dart:142:5)
#1      Controller.handle (package:myapp/controller.dart:30:12)
#2      main (file:///app/bin/main.dart:20:3)
#3      _delayEntrypointInvocation.<anonymous closure> (dart:isolate-patch/isolate_patch.dart:300:19)
"""

PERL = """\
boom at /app/Gateway.pm line 12.
\tGateway::charge(100) called at /app/Controller.pm line 30
\tController::handle("Controller=HASH(0x1)") called at /app/index.pl line 10
"""

R_TRACE = """\
Error in charge(100) : boom
Calls: main -> handle -> charge
Traceback:
3: stop("boom") at gateway.R#12
2: charge(100) at gateway.R#30
1: handle(req) at /usr/lib/R/library/shiny/R/app.R#8
"""

MATLAB = """\
Error using gateway>charge (line 12)
Insufficient funds.

Error in controller>handle (line 30)
    gateway.charge(100)

Error in main (line 5)
    controller.handle()
"""

SHELL = "./deploy.sh: line 42: kubectl: command not found"

CPP_GDB = """\
Program received signal SIGSEGV, Segmentation fault.
#0  0x00005555555551a9 in charge (amount=5) at gateway.c:142
#1  0x0000555555555200 in main () at main.c:20
"""

CPP_ASAN = """\
==1234==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000014
READ of size 4 at 0x602000000014 thread T0
    #0 0x4d1b2c in Gateway::charge(int) /app/gateway.cpp:42:5
    #1 0x4d1c3d in main /app/main.cpp:10:3
    #2 0x7f5e in __libc_start_main /build/glibc-abc/csu/../csu/libc-start.c:308:16
"""


def test_scala_is_read_as_a_jvm_trace() -> None:
    out = parse_stack_trace(SCALA)
    assert out.language == "java"  # Scala and Kotlin run on the JVM: same format
    create, anon, library = out.frames
    assert create.path_hint == "com/log0/billing/Invoices.scala"  # "Invoices$" object -> Invoices.scala
    assert anon.function == "$anonfun$run$1" and anon.in_app
    assert library.in_app is False  # scala.* standard library


def test_csharp_frames_with_and_without_source_info() -> None:
    out = parse_stack_trace(CSHARP)
    assert out.language == "csharp"
    assert out.exception_type == "System.NullReferenceException"
    assert out.exception_message == "Object reference not set to an instance of an object."
    charge, main, framework = out.frames
    assert (charge.module, charge.function, charge.file, charge.line) == (
        "MyApp.Payments.Gateway", "Charge", "/app/Payments/Gateway.cs", 142,
    )
    assert charge.in_app and main.in_app
    assert framework.in_app is False and framework.file == "" and framework.line is None
    assert out.first_in_app_frame() == charge


def test_rust_panic_backtrace() -> None:
    out = parse_stack_trace(RUST)
    assert out.language == "rust"
    assert out.exception_type == "panic"
    assert out.exception_message == "index out of bounds: the len is 3 but the index is 5"
    assert [f.in_app for f in out.frames] == [False, False, True, True]
    charge = out.first_in_app_frame()
    assert charge is not None
    # the compiler's ::h<16 hex> hash is removed; ./ is stripped from the repo path guess
    assert (charge.module, charge.function, charge.line, charge.column) == ("myapp::gateway", "charge", 142, 9)
    assert charge.path_hint == "src/gateway.rs"


def test_rust_old_style_panic_message() -> None:
    text = "thread 'main' panicked at 'boom', src/main.rs:5:1\n   0: myapp::run\n             at ./src/main.rs:5:1\n"
    out = parse_stack_trace(text)
    assert (out.exception_type, out.exception_message) == ("panic", "boom")


def test_ruby_frames_and_header_on_the_first_line() -> None:
    out = parse_stack_trace(RUBY)
    assert out.language == "ruby"
    assert (out.exception_type, out.exception_message) == ("RuntimeError", "boom")
    charge, block, gem = out.frames
    assert (charge.module, charge.function, charge.line) == ("Gateway", "charge", 142)  # Ruby 3.4 'Class#method'
    assert block.function == "<main>"
    assert gem.in_app is False and out.first_in_app_frame() == charge


def test_php_pairs_each_location_with_the_function_running_there() -> None:
    out = parse_stack_trace(PHP)
    assert out.language == "php"
    assert (out.exception_type, out.exception_message) == ("Exception", "boom")
    # PHP prints call sites; frames are re-aligned to "function, at file:line"
    assert [(f.function, f.file, f.line) for f in out.frames] == [
        ("charge", "/app/Gateway.php", 12),  # the throw site, taken from the header
        ("handle", "/app/Controller.php", 30),
        ("run", "/app/vendor/slim/slim/App.php", 55),
        ("{main}", "/app/index.php", 10),
    ]
    assert [f.in_app for f in out.frames] == [True, True, False, True]  # /vendor/ is library
    assert out.frames[0].module == "Gateway" and out.frames[2].module == "Slim\\App"


def test_php_without_a_throw_site_drops_the_unknown_innermost_body() -> None:
    out = parse_stack_trace("#0 /app/Controller.php(30): Gateway->charge(100)\n#1 {main}")
    assert [(f.function, f.line) for f in out.frames] == [("{main}", 30)]


def test_swift_symbolicated_crash_log_skips_unsymbolicated_frames() -> None:
    out = parse_stack_trace(SWIFT)
    assert out.language == "swift"
    assert (out.exception_type, out.exception_message) == ("Fatal error", "Index out of range")
    charge, main = out.frames  # the libswiftCore line has no file:line, so it is not a frame
    assert (charge.module, charge.function, charge.file, charge.line) == ("Gateway", "charge", "Gateway.swift", 142)
    assert main.function == "main" and main.module is None and main.in_app


def test_swift_linux_backtrace() -> None:
    out = parse_stack_trace("#0 0x000055d1 in Gateway.charge() at /app/Sources/Gateway.swift:142:9")
    assert out.language == "swift"  # not mistaken for gdb: the file ends in .swift
    assert (out.frames[0].function, out.frames[0].line, out.frames[0].column) == ("charge", 142, 9)


def test_dart_frames_and_exception() -> None:
    out = parse_stack_trace(DART)
    assert out.language == "dart"
    assert (out.exception_type, out.exception_message) == ("Bad state", "Insufficient funds")
    charge, _handle, main, core = out.frames
    assert (charge.module, charge.function, charge.line, charge.column) == ("Gateway", "charge", 142, 5)
    assert charge.path_hint == "lib/gateway.dart"  # package:myapp/ -> lib/
    assert main.path_hint == "/app/bin/main.dart" and main.module is None
    assert core.in_app is False  # dart: core library
    assert out.first_in_app_frame() == charge


def test_perl_confess_trace_realigned_to_executing_functions() -> None:
    out = parse_stack_trace(PERL)
    assert out.language == "perl"
    assert (out.exception_type, out.exception_message) == ("die", "boom")
    assert [(f.module, f.function, f.file, f.line) for f in out.frames] == [
        ("Gateway", "charge", "/app/Gateway.pm", 12),
        ("Controller", "handle", "/app/Controller.pm", 30),
        (None, "<main>", "/app/index.pl", 10),
    ]


def test_perl_bare_die_message_is_a_single_frame() -> None:
    out = parse_stack_trace("Can't locate object method \"x\" at /app/run.pl line 7.")
    assert out.language == "perl" and (out.frames[0].file, out.frames[0].line) == ("/app/run.pl", 7)


def test_r_traceback_with_source_references() -> None:
    out = parse_stack_trace(R_TRACE)
    assert out.language == "r"
    assert (out.exception_type, out.exception_message) == ("Error", "boom")
    assert [(f.function, f.file, f.line) for f in out.frames] == [
        ("stop", "gateway.R", 12),
        ("charge", "gateway.R", 30),
        ("handle", "/usr/lib/R/library/shiny/R/app.R", 8),
    ]
    assert [f.in_app for f in out.frames] == [True, True, False]


def test_matlab_error_stack() -> None:
    out = parse_stack_trace(MATLAB)
    assert out.language == "matlab"
    assert (out.exception_type, out.exception_message) == ("Error", "Insufficient funds.")
    assert [(f.function, f.file, f.line) for f in out.frames] == [
        ("charge", "gateway.m", 12),  # "gateway>charge": function charge in gateway.m
        ("handle", "controller.m", 30),
        ("main", "main.m", 5),
    ]


def test_shell_error_line() -> None:
    out = parse_stack_trace(SHELL)
    assert out.language == "shell"
    assert (out.exception_type, out.exception_message) == ("shell error", "kubectl: command not found")
    assert (out.frames[0].file, out.frames[0].line, out.frames[0].path_hint) == ("./deploy.sh", 42, "deploy.sh")


def test_non_shell_file_line_messages_are_not_shell_frames() -> None:
    assert parse_stack_trace("config.yml: line 5: mapping values are not allowed here").frames == []


def test_cpp_gdb_backtrace() -> None:
    out = parse_stack_trace(CPP_GDB)
    assert out.language == "cpp"
    assert (out.exception_type, out.exception_message) == ("SIGSEGV", "Segmentation fault")
    assert [(f.function, f.file, f.line) for f in out.frames] == [("charge", "gateway.c", 142), ("main", "main.c", 20)]


def test_cpp_address_sanitizer_trace() -> None:
    out = parse_stack_trace(CPP_ASAN)
    assert out.language == "cpp"
    assert out.exception_type == "AddressSanitizer"
    assert out.exception_message is not None and out.exception_message.startswith("heap-buffer-overflow")
    charge, _main, libc = out.frames
    assert (charge.module, charge.function, charge.line, charge.column) == ("Gateway", "charge(int)", 42, 5)
    assert libc.in_app is False  # glibc


def test_cpp_unsymbolised_frames_keep_the_function_but_claim_no_location() -> None:
    out = parse_stack_trace(
        "./app(charge+0x1d) [0x4005d1]\n"
        "./app(_ZN7Gateway6chargeEi+0x2f) [0x4006a0]\n"
        "/lib/x86_64-linux-gnu/libc.so.6(__libc_start_main+0xf3) [0x7f3a1c0290b3]\n"
        "./app(+0x1a2b) [0x55d0c3a01a2b]\n"  # address only: nothing usable, skipped
        "/lib/x86_64-linux-gnu/libc.so.6(+0x2a1ca) [0x7f3a1c02a1ca]"  # no function either
    )
    assert out.language == "cpp"
    charge, mangled, _libc = out.frames
    assert (charge.function, charge.file, charge.line, charge.path_hint) == ("charge", "", None, None)
    assert mangled.function == "_ZN7Gateway6chargeEi"  # kept as printed, not guessed at
    assert [f.in_app for f in out.frames] == [True, True, False]  # libc.so is library
    assert not charge.has_location and out.first_in_app_frame() == charge  # find_symbol("charge")


def test_symbolised_frames_report_a_location() -> None:
    out = parse_stack_trace(CPP_GDB)
    assert all(f.has_location for f in out.frames)
    unlocated = parse_stack_trace("   at System.Threading.Tasks.Task.Execute()").frames[0]
    assert not unlocated.has_location  # C# framework frames have no source either


def test_mixed_symbolised_and_unsymbolised_frames_are_all_kept() -> None:
    out = parse_stack_trace(CPP_GDB + "./app(helper+0x10) [0x400100]\n")
    assert out.language == "cpp" and len(out.frames) == 3
    assert [f.has_location for f in out.frames] == [True, True, False]


def test_javascript_firefox_and_safari_format() -> None:
    out = parse_stack_trace(
        "charge@https://app.example.com/static/app.js:142:15\nhandle@https://app.example.com/static/app.js:20:7"
    )
    assert out.language == "node"
    assert [(f.function, f.line, f.column) for f in out.frames] == [("charge", 142, 15), ("handle", 20, 7)]


def test_typescript_is_javascript_with_ts_paths() -> None:
    out = parse_stack_trace("Error: boom\n    at charge (/app/src/gateway.ts:142:15)")
    assert out.language == "node" and out.frames[0].file.endswith("gateway.ts")


def test_similar_looking_formats_are_not_confused() -> None:
    # C# frames "at A.B(Int32 x)" must not be read as Java ...
    assert parse_stack_trace("   at MyApp.Gateway.Charge(Int32 amount)").language == "csharp"
    # ... a Java frame without debug info must stay Java ...
    assert parse_stack_trace("\tat com.log0.A.run(Unknown Source)").language == "java"
    # ... and a Java frame must not be read as C#
    assert parse_stack_trace("\tat com.log0.A.run(A.java:5)").language == "java"
    # Dart "#0 fn (package:x/y.dart:1:2)" is not gdb, PHP or Swift
    assert parse_stack_trace("#0      run (package:app/a.dart:1:2)").language == "dart"
