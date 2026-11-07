"""Deterministic stack-trace parsing.

Languages: Java/Kotlin/Scala (JVM), Python, JavaScript/TypeScript (Node, Chrome, Firefox,
Safari), Go, Rust, C#/.NET, Ruby, PHP, Swift, Dart, Perl, R, MATLAB, shell (bash error
lines) and C/C++ (full file:line when symbolised; function-only frames otherwise).

log0 stores the stack trace of an error in the ``trace_id`` column of
``log_events``. This module turns that text into typed frames so the code
investigator can start from the failing file and line, instead of asking an LLM
to guess. It never raises: unparseable input yields a result with no frames.
"""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel

Language = Literal[
    "java", "python", "node", "go", "rust", "csharp", "ruby", "php", "swift",
    "dart", "perl", "r", "matlab", "shell", "cpp",
]

MAX_INPUT_CHARS = 20_000
MAX_FRAMES = 50
MAX_MESSAGE_CHARS = 500
MAX_CAUSED_BY = 5

_JAVA_LIBRARY_PREFIXES = (
    "java.", "javax.", "jakarta.", "jdk.", "sun.", "com.sun.", "kotlin.", "kotlinx.",
    "scala.", "org.springframework.", "org.apache.", "org.hibernate.", "io.netty.",
    "reactor.", "com.zaxxer.", "ch.qos.", "org.slf4j.", "io.micrometer.",
)
_CSHARP_LIBRARY_PREFIXES = ("System.", "Microsoft.", "Newtonsoft.", "Azure.", "Npgsql.")
_GO_LIBRARY_MARKERS = ("/usr/local/go/", "/go/pkg/mod/", "/vendor/", "/src/runtime/")
_RUST_LIBRARY_PREFIXES = ("std::", "core::", "alloc::", "tokio::", "futures::", "hyper::", "rust_begin_unwind")
_SWIFT_LIBRARY_BINARIES = ("libswift", "libsystem", "libdispatch", "libc++", "Foundation", "CoreFoundation", "libobjc")
_NATIVE_LIBRARY_MARKERS = ("/usr/include/", "/usr/lib/", "/build/glibc", "/build/gcc", "/rustc/")


class StackFrame(BaseModel):
    language: Language
    function: str  # method / function name
    module: str | None  # Java/C# class, Go package, PHP class, Rust path; else None
    file: str  # exactly as printed ("" when the runtime printed none)
    line: int | None
    column: int | None = None
    path_hint: str | None  # best guess of the path inside the repo (matched by suffix later)
    in_app: bool  # False for the JDK, frameworks, site-packages, node_modules, stdlibs, ...

    @property
    def has_location(self) -> bool:
        """True when the frame names a file and line. False means "only a symbol is known"
        (framework frames in C#, unsymbolised C/C++): search the repo for the function instead."""
        return bool(self.file) and self.line is not None


class ParsedStackTrace(BaseModel):
    language: Language | None
    exception_type: str | None
    exception_message: str | None
    caused_by: list[str]  # "Type: message" for each Java "Caused by:" line
    frames: list[StackFrame]  # innermost call (where it failed) first, for every language
    truncated: bool  # more than MAX_FRAMES frames were present

    def first_in_app_frame(self) -> StackFrame | None:
        """The top-most frame in the user's own code: the best first place to look."""
        return next((f for f in self.frames if f.in_app), None)


# ------------------------------------------------------------------ Java / Kotlin

_JAVA_FRAME = re.compile(
    r"^\s*(?:at\s+)?"
    r"(?P<symbol>[A-Za-z_$][\w$.<>/@\-]*\.[\w$<>\-]+)"
    r"\((?P<file>[^():]+)(?::(?P<line>\d+))?\)\s*$"
)
# A real JVM frame names a source file or one of the JVM's placeholders. This is what keeps
# C# frames such as "at A.B.C(Int32 x)" from being read as Java.
_JAVA_FILE = re.compile(
    r"^(?:[\w$.\-]+\.(?:java|kt|kts|scala|groovy|clj)|Native Method|Unknown Source|Generated Source|<generated>)$"
)
_JAVA_EXCEPTION = re.compile(r"^(?P<type>[A-Za-z_$][\w$.]*)(?::\s*(?P<msg>.*))?$")
_JAVA_SOURCE_EXTENSIONS = (".java", ".kt", ".scala", ".groovy")


def _parse_java(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        m = _JAVA_FRAME.match(text)
        if not m or not _JAVA_FILE.match(m["file"].strip()):
            continue
        symbol = m["symbol"].split("/")[-1]  # drop a Java 9+ "java.base/" module prefix
        cls, _, method = symbol.rpartition(".")
        file = m["file"].strip()
        has_source = file.endswith(_JAVA_SOURCE_EXTENSIONS)
        extension = file[file.rfind("."):] if has_source else ".java"
        top_level = cls.split("$")[0]  # com.x.Outer$Inner -> com.x.Outer
        frames.append(
            StackFrame(
                language="java",
                function=method,
                module=cls or None,
                file=file,
                line=int(m["line"]) if m["line"] else None,
                path_hint=top_level.replace(".", "/") + extension if top_level and has_source else None,
                in_app=not cls.startswith(_JAVA_LIBRARY_PREFIXES),
            )
        )
    return frames


# ------------------------------------------------------------------------ Python

_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+?))?\s*$')
_PY_EXCEPTION = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s*(?P<msg>.*))?$")


def _parse_python(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        m = _PY_FRAME.match(text)
        if not m:
            continue
        file = m["file"].replace("\\", "/")
        library = "site-packages" in file or "dist-packages" in file or "/lib/python" in file or file.startswith("<")
        frames.append(
            StackFrame(
                language="python",
                function=m["func"] or "<unknown>",
                module=None,
                file=file,
                line=int(m["line"]),
                path_hint=None if file.startswith("<") else file,
                in_app=not library,
            )
        )
    frames.reverse()  # Python prints the most recent call LAST; we want innermost first
    return frames


# ------------------------------------------------- JavaScript / TypeScript (Node, browsers)

_NODE_FRAME = re.compile(  # V8 (Node, Chrome, Edge): "at fn (file:line:col)" / "at file:line:col"
    r"^\s*at\s+(?:async\s+)?"
    r"(?:(?P<func>[^\s(][^(]*?)\s+\()?"
    r"(?P<file>[^()\s]+?):(?P<line>\d+)(?::(?P<col>\d+))?\)?\s*$"
)
_JS_GECKO_FRAME = re.compile(  # Firefox / Safari: "fn@file:line:col"
    r"^\s*(?P<func>[^@\s]*)@(?P<file>\S+?):(?P<line>\d+):(?P<col>\d+)\s*$"
)
_NODE_EXCEPTION = re.compile(r"^(?P<type>[A-Za-z_$][\w$.]*(?:Error|Exception))(?::\s*(?P<msg>.*))?$")


def _parse_node(lines: list[str]) -> list[StackFrame]:
    frames = []
    previous = ""
    for text in lines:
        after_rust_symbol = bool(_RUST_FRAME.match(previous))  # Rust prints "N: symbol" then "at file:line"
        previous = text
        m = _NODE_FRAME.match(text)
        if m and (_JAVA_FRAME.match(text) or after_rust_symbol):
            m = None
        if not m:
            m = _JS_GECKO_FRAME.match(text)
        if not m:
            continue
        file = m["file"].removeprefix("file://").replace("\\", "/")
        library = "node_modules" in file or file.startswith(("node:", "internal/")) or "webpack/runtime" in file
        frames.append(
            StackFrame(
                language="node",
                function=(m["func"] or "<anonymous>").strip(),
                module=None,
                file=file,
                line=int(m["line"]),
                column=int(m["col"]) if m["col"] else None,
                path_hint=None if file.startswith("node:") else file,
                in_app=not library,
            )
        )
    return frames


# ---------------------------------------------------------------------------- Go

_GO_FILE = re.compile(r"^\s+(?P<file>\S+\.go):(?P<line>\d+)(?:\s+\+0x[0-9a-fA-F]+)?\s*$")
_GO_FUNC = re.compile(r"^(?P<call>[\w./*()\-]+\([^\n]*\))\s*$")
_GO_PANIC = re.compile(r"^(?P<type>panic|fatal error):\s*(?P<msg>.*)$")


def _parse_go(lines: list[str]) -> list[StackFrame]:
    frames = []
    for previous, current in pairwise(lines):
        file_match = _GO_FILE.match(current)
        func_match = _GO_FUNC.match(previous)
        if not (file_match and func_match):
            continue
        full = func_match["call"].rsplit("(", 1)[0]  # drop the argument list
        function = full.rsplit(".", 1)[-1]
        module = re.sub(r"\.\(\*?\w+\)$", "", full[: -len(function) - 1]) or None  # drop "(*Server)"
        file = file_match["file"].replace("\\", "/")
        library = any(marker in file for marker in _GO_LIBRARY_MARKERS)
        frames.append(
            StackFrame(
                language="go",
                function=function,
                module=module,
                file=file,
                line=int(file_match["line"]),
                path_hint=file,
                in_app=not library,
            )
        )
    return frames


# -------------------------------------------------------------------------- Rust

_RUST_FRAME = re.compile(r"^\s*\d+:\s+(?P<symbol>\S.*?)\s*$")
_RUST_AT = re.compile(r"^\s+at\s+(?P<file>\S+?):(?P<line>\d+)(?::(?P<col>\d+))?\s*$")
_RUST_HASH = re.compile(r"::h[0-9a-f]{16}$")
_RUST_PANIC = re.compile(
    r"^thread '[^']*' (?:\(\d+\) )?panicked at (?:'(?P<old>.*)', )?(?P<loc>\S+?:\d+(?::\d+)?):?$"
)


def _parse_rust(lines: list[str]) -> list[StackFrame]:
    frames = []
    for previous, current in pairwise(lines):
        at = _RUST_AT.match(current)
        sym = _RUST_FRAME.match(previous)
        if not (at and sym):
            continue
        symbol = _RUST_HASH.sub("", sym["symbol"])  # drop the compiler's ::h0123456789abcdef
        module, _, function = symbol.rpartition("::")
        file = at["file"].replace("\\", "/")
        library = symbol.startswith(_RUST_LIBRARY_PREFIXES) or any(m in file for m in ("/rustc/", "/.cargo/registry/"))
        frames.append(
            StackFrame(
                language="rust",
                function=function or symbol,
                module=module or None,
                file=file,
                line=int(at["line"]),
                column=int(at["col"]) if at["col"] else None,
                path_hint=file.removeprefix("./"),
                in_app=not library,
            )
        )
    return frames


# -------------------------------------------------------------------------- C# / .NET

_CS_FRAME = re.compile(
    r"^\s*at\s+(?P<symbol>[\w.<>`+\[\],$]+)\((?P<args>[^():]*)\)"
    r"(?:\s+in\s+(?P<file>.+?):line\s+(?P<line>\d+))?\s*$"
)


def _parse_csharp(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        m = _CS_FRAME.match(text)
        if not m or m["args"].strip() in ("Native Method", "Unknown Source"):
            continue  # JVM placeholders: not a .NET frame
        cls, _, method = m["symbol"].rpartition(".")
        file = (m["file"] or "").replace("\\", "/")
        frames.append(
            StackFrame(
                language="csharp",
                function=method or m["symbol"],
                module=cls or None,
                file=file,
                line=int(m["line"]) if m["line"] else None,
                path_hint=file or None,
                in_app=not m["symbol"].startswith(_CSHARP_LIBRARY_PREFIXES),
            )
        )
    return frames


# -------------------------------------------------------------------------- Ruby

_RUBY_FRAME = re.compile(
    r"^\s*(?:from\s+)?(?P<file>[^\s:]+\.rb):(?P<line>\d+):in\s+[`'](?P<func>[^'`]+)'"
    r"(?::\s*(?P<msg>.*?)\s+\((?P<type>[\w:]+)\))?\s*$"
)


def _parse_ruby(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        m = _RUBY_FRAME.match(text)
        if not m:
            continue
        file = m["file"].replace("\\", "/")
        library = "/gems/" in file or "/lib/ruby/" in file or file.startswith("<internal:")
        module, _, function = m["func"].replace("block in ", "").rpartition("#")  # Ruby 3.4: 'Gateway#charge'
        frames.append(
            StackFrame(
                language="ruby",
                function=function or m["func"],
                module=module or None,
                file=file,
                line=int(m["line"]),
                path_hint=file,
                in_app=not library,
            )
        )
    return frames


# --------------------------------------------------------------- call-site languages
# PHP and Perl print CALL SITES: "file(line): callee(args)" means callee was invoked at
# file:line, which is *inside the next frame's function*. This helper pairs each location
# with the function that was actually executing there, so frames mean the same thing as
# in every other language ("function X, at file:line").


def _executing_frames(
    throw_site: tuple[str, int] | None,
    calls: list[tuple[str, str, int]],
    top_level: str,
) -> list[tuple[str, str, int]]:
    """calls: innermost-first [(callee, call_file, call_line)] -> [(function, file, line)]."""
    functions = [c[0] for c in calls] + [top_level]
    locations = [(c[1], c[2]) for c in calls]
    if throw_site:
        locations = [throw_site, *locations]  # where the exception was raised, inside calls[0]
    else:
        functions = functions[1:]  # without the throw site, calls[0]'s own body is unknown
    return [(fn, file, line) for fn, (file, line) in zip(functions, locations, strict=True)]


def _split_callee(callee: str) -> tuple[str | None, str]:
    parts = re.split(r"->|::", callee)
    return (parts[0], parts[-1]) if len(parts) > 1 else (None, callee)


# ---------------------------------------------------------------------------- PHP

_PHP_FRAME = re.compile(r"^#\d+\s+(?P<file>[^(\s][^(]*)\((?P<line>\d+)\):\s+(?P<call>.+?)\s*$")
_PHP_EXCEPTION = re.compile(
    r"^(?:PHP\s+)?(?:Fatal error|Warning):?\s+Uncaught\s+(?P<type>[\w\\]+):\s*(?P<msg>.*?)"
    r"(?:\s+in\s+(?P<file>\S+?):(?P<line>\d+))?$"
)


def _parse_php(lines: list[str]) -> list[StackFrame]:
    calls = []
    for text in lines:
        m = _PHP_FRAME.match(text)
        if m:
            calls.append((m["call"].split("(", 1)[0], m["file"].replace("\\", "/"), int(m["line"])))
    if not calls:
        return []
    header = next((m for t in lines if (m := _PHP_EXCEPTION.match(t.strip())) and m["file"]), None)
    throw_site = (header["file"], int(header["line"])) if header else None
    frames = []
    for callee, file, line in _executing_frames(throw_site, calls, "{main}"):
        module, function = _split_callee(callee)
        frames.append(
            StackFrame(
                language="php", function=function, module=module, file=file, line=line,
                path_hint=file, in_app="/vendor/" not in file,
            )
        )
    return frames


# ---------------------------------------------------------------------------- Perl

_PERL_HEADER = re.compile(r"^(?P<msg>.*?)\s+at\s+(?P<file>\S+\.(?:pl|pm|t|psgi|cgi))\s+line\s+(?P<line>\d+)\.?$")
_PERL_CALL = re.compile(
    r"^\s*(?P<call>[\w:]+)\((?P<args>.*)\)\s+called at\s+(?P<file>\S+)\s+line\s+(?P<line>\d+)\s*$"
)


def _parse_perl(lines: list[str]) -> list[StackFrame]:
    calls = [
        (m["call"], m["file"].replace("\\", "/"), int(m["line"]))
        for t in lines
        if (m := _PERL_CALL.match(t))
    ]
    header = next((m for t in lines if t and not t[0].isspace() and (m := _PERL_HEADER.match(t.strip()))), None)
    if not calls and not header:
        return []
    throw_site = (header["file"], int(header["line"])) if header else None
    if not calls:  # a bare "died at file line N." has no callers
        return [
            StackFrame(
                language="perl", function="<unknown>", module=None, file=header["file"],
                line=int(header["line"]), path_hint=header["file"], in_app=True,
            )
        ]
    frames = []
    for callee, file, line in _executing_frames(throw_site, calls, "<main>"):
        module, _, function = callee.rpartition("::")
        library = any(x in file for x in ("/perl5/", "/site_perl/", "/usr/share/perl", "/usr/lib/perl"))
        frames.append(
            StackFrame(
                language="perl", function=function or callee, module=module or None,
                file=file, line=line, path_hint=file, in_app=not library,
            )
        )
    return frames


# -------------------------------------------------------------------------- Swift

_SWIFT_CRASH = re.compile(  # "0  MyApp  0x000000010 Gateway.charge() + 52 (Gateway.swift:142)"
    r"^\s*\d+\s+(?P<binary>\S+)\s+0x[0-9a-fA-F]+\s+(?P<symbol>.+?)"
    r"(?:\s+\+\s+\d+)?\s+\((?P<file>[^():]+\.swift):(?P<line>\d+)\)\s*$"
)
_SWIFT_LINUX = re.compile(  # "#0 0x000055 in Gateway.charge() at /app/Sources/Gateway.swift:142"
    r"^\s*#\d+\s+(?:0x[0-9a-fA-F]+\s+in\s+)?(?P<symbol>.+?)\s+at\s+(?P<file>\S+\.swift):(?P<line>\d+)(?::(?P<col>\d+))?\s*$"
)
_SWIFT_FATAL = re.compile(r"^(?:.*?)Fatal error:\s*(?P<msg>.*?)(?::\s*file\s+\S+\.swift,\s+line\s+\d+)?$")


def _parse_swift(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        crash = _SWIFT_CRASH.match(text)
        linux = None if crash else _SWIFT_LINUX.match(text)
        m = crash or linux
        if not m:
            continue
        symbol = m["symbol"].strip()
        function = symbol.split("(")[0].rsplit(".", 1)[-1]
        module = symbol.split("(")[0].rpartition(".")[0] or None
        library = bool(crash and crash["binary"].startswith(_SWIFT_LIBRARY_BINARIES)) or symbol.startswith("Swift.")
        frames.append(
            StackFrame(
                language="swift",
                function=function,
                module=module,
                file=m["file"],
                line=int(m["line"]),
                column=int(linux["col"]) if linux and linux["col"] else None,
                path_hint=m["file"],
                in_app=not library,
            )
        )
    return frames


# -------------------------------------------------------------------------- Dart

_DART_FRAME = re.compile(
    r"^#\d+\s+(?P<func>.+?)\s+\((?P<file>\S+?\.dart):(?P<line>\d+)(?::(?P<col>\d+))?\)\s*$"
)
_DART_EXCEPTION = re.compile(
    r"^(?:Unhandled [Ee]xception:\s*)?"
    r"(?P<type>[A-Za-z_]\w*(?:Exception|Error)|Bad state|Invalid argument\(s\))(?::\s*(?P<msg>.*))?$"
)


def _parse_dart(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        m = _DART_FRAME.match(text)
        if not m:
            continue
        file = m["file"].replace("\\", "/")
        func = m["func"].strip()
        module, _, function = func.rpartition(".")
        library = file.startswith(("dart:", "package:flutter/"))
        frames.append(
            StackFrame(
                language="dart",
                function=function or func,
                module=module or None,
                file=file,
                line=int(m["line"]),
                column=int(m["col"]) if m["col"] else None,
                path_hint=None
                if file.startswith("dart:")
                else re.sub(r"^package:[^/]+/", "lib/", file.removeprefix("file://")),
                in_app=not library,
            )
        )
    return frames


# ---------------------------------------------------------------------------- R

_R_FRAME = re.compile(r"^\s*\d+:\s+(?P<call>.+?)\s+at\s+(?P<file>\S+\.[Rr])#(?P<line>\d+)\s*$")
_R_ERROR = re.compile(r"^Error(?: in (?P<call>.+?)\s+:|:)\s*(?P<msg>.*)$")


def _parse_r(lines: list[str]) -> list[StackFrame]:
    frames = []  # traceback() already lists the innermost call first
    for text in lines:
        m = _R_FRAME.match(text)
        if not m:
            continue
        file = m["file"].replace("\\", "/")
        call = m["call"]
        frames.append(
            StackFrame(
                language="r", function=call.split("(", 1)[0] or call, module=None, file=file,
                line=int(m["line"]), path_hint=file,
                in_app="/library/" not in file and "/R/library" not in file,
            )
        )
    return frames


# ------------------------------------------------------------------------- MATLAB

_MATLAB_FRAME = re.compile(
    r"^\s*(?:>\s*)?(?P<kind>Error using|Error in|In)\s+(?P<symbol>[^\s(]+)\s+\(line\s+(?P<line>\d+)\)\s*$"
)


def _parse_matlab(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        m = _MATLAB_FRAME.match(text)
        if not m:
            continue
        symbol = m["symbol"]
        if symbol.endswith(".m"):  # a full path was printed
            file = symbol.replace("\\", "/")
            function = file.rsplit("/", 1)[-1].removesuffix(".m")
        else:
            file_part, _, local = symbol.partition(">")  # "gateway>charge": a function in gateway.m
            function = (local or file_part).split(">")[-1]
            file = file_part + ".m"
        frames.append(
            StackFrame(
                language="matlab", function=function, module=None, file=file, line=int(m["line"]),
                path_hint=file, in_app="/toolbox/" not in file and not file.startswith("matlab."),
            )
        )
    return frames


# -------------------------------------------------------------------------- Shell
# Shell has no standard stack trace. The one stable format is bash's own error line:
#   ./deploy.sh: line 42: kubectl: command not found

_SHELL_LINE = re.compile(r"^(?P<file>[^\s:]+): line (?P<line>\d+): (?P<msg>.*)$")
_SHELL_NAMES = ("bash", "sh", "zsh", "dash", "ksh")


def _is_shell_path(file: str) -> bool:
    return file.endswith((".sh", ".bash", ".zsh", ".ksh", ".bats")) or file in _SHELL_NAMES or file.startswith(("./", "/"))


def _parse_shell(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        m = _SHELL_LINE.match(text)
        if not m or not _is_shell_path(m["file"]):  # "config.yml: line 5: ..." is not a shell error
            continue
        file = m["file"]
        frames.append(
            StackFrame(
                language="shell", function="<script>", module=None, file=file, line=int(m["line"]),
                path_hint=None if file in _SHELL_NAMES else file.removeprefix("./"), in_app=True,
            )
        )
    return frames


# ------------------------------------------------------------------------ C / C++

_NATIVE_GDB = re.compile(  # "#0  0x7f in charge (amount=5) at gateway.c:142"
    r"^#\d+\s+(?:0x[0-9a-fA-F]+\s+in\s+)?(?P<func>[^\s(]+)\s*\((?P<args>.*?)\)\s+at\s+(?P<file>[^\s:]+):(?P<line>\d+)\s*$"
)
_NATIVE_SANITIZER = re.compile(  # "    #1 0x55 in charge /app/gateway.cpp:42:5"
    r"^\s*#\d+\s+0x[0-9a-fA-F]+\s+in\s+(?P<func>.+?)\s+(?P<file>/?[^\s:()]+\.(?:c|cc|cpp|cxx|h|hh|hpp|m|mm)):(?P<line>\d+)(?::(?P<col>\d+))?\s*$"
)
_NATIVE_UNSYMBOLISED = re.compile(  # glibc backtrace_symbols: "./app(charge+0x1d) [0x4005d1]"
    r"^(?P<binary>[^\s()\[\]]+)\((?P<func>[A-Za-z_][^\s()+]*)\+0x[0-9a-fA-F]+\)\s+\[0x[0-9a-fA-F]+\]\s*$"
)
_NATIVE_HEADER = re.compile(
    r"^(?:==\d+==\s*)?(?:ERROR:\s*(?P<san>\w+Sanitizer):\s*(?P<smsg>.*)|"
    r"Program received signal (?P<sig>\w+),\s*(?P<gmsg>.*?)\.?|"
    r"terminate called after throwing an instance of '(?P<cxx>[^']+)')$"
)


def _unsymbolised_native_frame(m: re.Match[str]) -> StackFrame:
    """A frame with a function name but no file or line (no debug info in the binary).

    Not a place to open a file, but a symbol the investigator can search for with
    ``find_symbol``. The name is kept as printed (it may be mangled, e.g. ``_ZN7Gateway6chargeEi``).
    """
    binary = m["binary"]
    library = ".so" in binary or binary.startswith(("/lib", "/usr/lib"))
    return StackFrame(
        language="cpp", function=m["func"], module=None, file="", line=None,
        path_hint=None, in_app=not library,
    )


def _parse_native(lines: list[str]) -> list[StackFrame]:
    frames = []
    for text in lines:
        if unsymbolised := _NATIVE_UNSYMBOLISED.match(text.strip()):
            frames.append(_unsymbolised_native_frame(unsymbolised))
            continue
        m = _NATIVE_GDB.match(text) or _NATIVE_SANITIZER.match(text)
        if not m:
            continue
        func = m["func"]
        module, _, function = func.rpartition("::")
        file = m["file"].replace("\\", "/")
        library = any(marker in file for marker in _NATIVE_LIBRARY_MARKERS) or func.startswith(("std::", "__"))
        frames.append(
            StackFrame(
                language="cpp",
                function=function or func,
                module=module or None,
                file=file,
                line=int(m["line"]),
                column=int(m.groupdict().get("col") or 0) or None,
                path_hint=file,
                in_app=not library,
            )
        )
    return frames


# ------------------------------------------------------------------------ driver


def _normalise(text: str) -> list[str]:
    text = text[:MAX_INPUT_CHARS]
    if "\n" not in text and "\\n" in text:  # newlines that were stored as the 2 characters \n
        text = text.replace("\\r", "").replace("\\n", "\n").replace("\\t", "\t")
    return text.replace("\r\n", "\n").split("\n")


def _first_exception(lines: list[str], pattern: re.Pattern[str], *, last: bool = False):
    """The exception header line: first match (most languages) or last match (Python)."""
    candidates = [
        m
        for line in lines
        if line and not line[0].isspace() and (m := pattern.match(line.strip()))
    ]
    if not candidates:
        return None
    return candidates[-1] if last else candidates[0]


def _exception_for(language: str, lines: list[str]) -> tuple[str | None, str | None]:
    """(type, message) of the error that started the trace, per language."""
    if language == "java":
        m = _first_exception(lines, _JAVA_EXCEPTION)
        return (m["type"], m["msg"]) if m else (None, None)
    if language == "python":
        m = _first_exception(lines, _PY_EXCEPTION, last=True)
        return (m["type"], m["msg"]) if m else (None, None)
    if language in ("node", "csharp"):
        m = _first_exception(lines, _NODE_EXCEPTION)
        return (m["type"], m["msg"]) if m else (None, None)
    if language == "go":
        m = _first_exception(lines, _GO_PANIC)
        return (m["type"], m["msg"]) if m else (None, None)
    if language == "ruby":
        m = next((m for t in lines if (m := _RUBY_FRAME.match(t)) and m["type"]), None)
        return (m["type"], m["msg"]) if m else (None, None)
    if language == "php":
        m = next((m for t in lines if (m := _PHP_EXCEPTION.match(t.strip()))), None)
        return (m["type"], m["msg"]) if m else (None, None)
    if language == "rust":
        for i, text in enumerate(lines):
            if m := _RUST_PANIC.match(text.strip()):
                message = m["old"] or (lines[i + 1].strip() if i + 1 < len(lines) else None)
                return "panic", message
        return None, None
    if language == "swift":
        m = next((m for t in lines if "Fatal error" in t and (m := _SWIFT_FATAL.match(t.strip()))), None)
        return ("Fatal error", m["msg"]) if m else (None, None)
    if language == "perl":
        m = next((m for t in lines if t and not t[0].isspace() and (m := _PERL_HEADER.match(t.strip()))), None)
        return ("die", m["msg"]) if m else (None, None)
    if language == "dart":
        m = _first_exception(lines, _DART_EXCEPTION)
        return (m["type"], m["msg"]) if m else (None, None)
    if language == "r":
        m = next((m for t in lines if (m := _R_ERROR.match(t.strip()))), None)
        return ("Error", m["msg"]) if m else (None, None)
    if language == "matlab":
        for i, text in enumerate(lines):
            if (m := _MATLAB_FRAME.match(text)) and m["kind"] == "Error using":
                return "Error", next((x.strip() for x in lines[i + 1 :] if x.strip()), None)
        return None, None
    if language == "shell":
        m = next((m for t in lines if (m := _SHELL_LINE.match(t)) and _is_shell_path(m["file"])), None)
        return ("shell error", m["msg"]) if m else (None, None)
    if language == "cpp":
        m = next((m for t in lines if (m := _NATIVE_HEADER.match(t.strip()))), None)
        if not m:
            return None, None
        if m["san"]:
            return m["san"], m["smsg"]
        if m["sig"]:
            return m["sig"], m["gmsg"]
        return m["cxx"], None
    return None, None


def parse_stack_trace(text: str | None) -> ParsedStackTrace:
    """Parse a raw stack trace. Never raises; unrecognised input has no frames."""
    empty = ParsedStackTrace(
        language=None, exception_type=None, exception_message=None,
        caused_by=[], frames=[], truncated=False,
    )
    if not text or not text.strip():
        return empty
    lines = _normalise(text)

    parsers = {
        "java": _parse_java(lines),
        "node": _parse_node(lines),
        "python": _parse_python(lines),
        "go": _parse_go(lines),
        "csharp": _parse_csharp(lines),
        "rust": _parse_rust(lines),
        "ruby": _parse_ruby(lines),
        "php": _parse_php(lines),
        "swift": _parse_swift(lines),
        "dart": _parse_dart(lines),
        "perl": _parse_perl(lines),
        "r": _parse_r(lines),
        "matlab": _parse_matlab(lines),
        "shell": _parse_shell(lines),
        "cpp": _parse_native(lines),
    }
    language, frames = max(parsers.items(), key=lambda item: len(item[1]))  # ties: first listed
    if not frames:
        return empty

    exception_type, message = _exception_for(language, lines)
    caused_by: list[str] = []
    if language == "java":
        caused_by = [
            line.split("Caused by:", 1)[1].strip()[:MAX_MESSAGE_CHARS]
            for line in lines
            if line.lstrip().startswith("Caused by:")
        ][:MAX_CAUSED_BY]

    return ParsedStackTrace(
        language=language,
        exception_type=exception_type,
        exception_message=(message or "")[:MAX_MESSAGE_CHARS] or None,
        caused_by=caused_by,
        frames=frames[:MAX_FRAMES],
        truncated=len(frames) > MAX_FRAMES,
    )
