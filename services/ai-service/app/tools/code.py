"""Code tools: list, search and read the files of a checkout (the investigator's view of the code).

``CodeTools`` works on one checkout folder (from ``RepoCache``, 3.4) and, optionally, one folder
inside it (a service's ``pathPrefix``). Three tools, all read-only and all bounded:

* ``list_files``: ``git ls-files`` (only files the repository tracks), optionally filtered by a glob.
  A glob without ``/`` (``*.py``) matches file names at any depth, like ``.gitignore``; a glob with
  ``/`` (``src/**/*.java``) is anchored at the scope. ``search_code`` uses the same rule.
* ``search_code``: ripgrep. It matches in linear time, so a pattern written by a model cannot
  freeze the server the way a backtracking regex can; it has file-size and line-length caps; and
  we stop reading its output once we have enough matches.
* ``read_file``: a path-guarded, line-numbered slice of at most 200 lines.

What they refuse, on purpose (``CodeToolError``, messages safe to hand back to the model):

* paths that leave the checkout (``..``, absolute paths, drive letters, backslashes, NUL, anything
  that resolves outside the root, the ``.git`` folder);
* files that probably hold secrets (``.env``, keys, certificates, ``.npmrc`` ...) and, for
  listing and searching, vendored or generated folders (``node_modules``, ``dist``, ``*.min.js``);
* binary files and files over 1 MB.

The result models are plain, typed objects. Turning them into evidence for the investigation is 3.8.
"""

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_LIST_LIMIT = 500
MAX_SEARCH_LIMIT = 50
MAX_CONTEXT_LINES = 5
MAX_READ_LINES = 200
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 400
MAX_PATTERN_CHARS = 200
MAX_GLOB_CHARS = 100
MAX_GLOB_WILDCARDS = 6
MAX_MATCHES_PER_FILE = 5
MAX_PATH_CHARS = 500
BINARY_SNIFF_BYTES = 8000
ERROR_DETAIL_CHARS = 200

# Never listed, searched or read. Vendored and generated folders are noise; the rest is secrets.
EXCLUDED_DIRS = frozenset({"node_modules", "vendor", "dist", "build", "target", "__pycache__", ".venv", "venv", ".gradle", ".idea", ".git"})
EXCLUDED_GLOBS = ("*.min.js", "*.min.css", "*.map", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock")
SENSITIVE_GLOBS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ed25519", "id_ed25519.*",
    ".npmrc", ".pypirc", ".netrc", "credentials.json", "secrets.json", "secrets.yml", "secrets.yaml",
)  # fmt: skip

_GLOB_CHARS = re.compile(r"^[A-Za-z0-9_.*?/\-]+$")  # no {a,b} or [a-z]: list_files and ripgrep must read a glob the same way
_DRIVE = re.compile(r"^[A-Za-z]:")
_SAFE_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR")


class CodeToolError(Exception):
    """A request the tools refuse or cannot serve. The message is safe to show to the model."""


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """``*`` stays inside one folder, ``**`` crosses folders, ``?`` is one character; the rest is literal."""
    out, i = [], 0
    while i < len(glob):
        char = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
        i += 1
    return re.compile("^" + "".join(out) + "$")


_EXCLUDED_RE = tuple(glob_to_regex(g) for g in EXCLUDED_GLOBS)
_SENSITIVE_RE = tuple(glob_to_regex(g) for g in SENSITIVE_GLOBS)


# --------------------------------------------------------------------------- results


class FileList(BaseModel):
    files: list[str]  # paths relative to the repository root, sorted
    total: int  # how many matched, before the limit
    truncated: bool


class CodeLine(BaseModel):
    number: int
    text: str
    match: bool  # True for a matching line, False for context around it


class CodeHit(BaseModel):
    """One run of consecutive lines (matches plus their context) in one file."""

    path: str
    first_line: int
    last_line: int
    lines: list[CodeLine]


class CodeSearchResult(BaseModel):
    pattern: str
    hits: list[CodeHit]
    matches: int
    truncated: bool  # stopped early: more matches exist than the limit


class FileSlice(BaseModel):
    path: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool  # the slice ends before the end of the file
    text: str  # "  12 | code", one numbered line per line


# ------------------------------------------------------------------------- guards


def is_sensitive(path: str) -> bool:
    name = PurePosixPath(path).name
    return any(pattern.match(name) for pattern in _SENSITIVE_RE)


def is_excluded(path: str) -> bool:
    """Vendored, generated or secret-bearing: not worth a model's attention, or not allowed at all."""
    parts = PurePosixPath(path).parts
    return (
        any(part.lower() in EXCLUDED_DIRS for part in parts[:-1])
        or any(pattern.match(parts[-1]) for pattern in _EXCLUDED_RE)
        or is_sensitive(path)
    )


def check_glob(glob: str) -> str:
    """Accept only plain glob characters, so a glob can never act as a git or ripgrep option."""
    if not glob or len(glob) > MAX_GLOB_CHARS or not _GLOB_CHARS.match(glob):
        raise CodeToolError("glob must be 1-100 characters of letters, digits and . _ - / * ?")
    if ".." in glob or glob.startswith("/") or glob.count("*") > MAX_GLOB_WILDCARDS:
        raise CodeToolError("glob must be relative, without '..' and with few wildcards")
    return glob


def check_repo_path(raw: str) -> str:
    """A repository-relative path in POSIX form, or ``CodeToolError``. Nothing here touches the disk."""
    path = raw.strip()
    if not path or len(path) > MAX_PATH_CHARS:
        raise CodeToolError("path must be 1-500 characters")
    if "\\" in path or "\x00" in path or any(ord(c) < 32 for c in path) or _DRIVE.match(path) or path.startswith("/"):
        raise CodeToolError("path must be relative to the repository root, with forward slashes")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CodeToolError("path must not contain '..', '.' or empty segments")
    if any(part.lower() == ".git" for part in parts):
        raise CodeToolError("the .git folder is not available")
    return path


def _child_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _SAFE_ENV if name in os.environ}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    return env


def _clip(text: str) -> str:
    text = text.rstrip("\r\n")
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + " ...[line cut]"


@dataclass(frozen=True)
class CodeTools:
    """The three tools over one checkout. ``prefix`` is a service's folder: the default scope."""

    root: Path
    prefix: str = ""
    git_binary: str = "git"
    rg_binary: str = "rg"
    search_timeout_seconds: float = 10.0
    list_timeout_seconds: float = 20.0

    # ------------------------------------------------------------------ list_files

    async def list_files(self, glob: str | None = None, *, limit: int = 100) -> FileList:
        """Tracked files under the scope, optionally filtered by ``glob`` (relative to the scope)."""
        limit = max(1, min(limit, MAX_LIST_LIMIT))
        matcher = glob_to_regex(check_glob(glob)) if glob else None
        any_depth = bool(glob) and "/" not in glob  # "*.py" matches at any depth, "src/*.py" is anchored at the scope
        raw = await asyncio.to_thread(self._git_ls_files)
        scope = f"{self.prefix}/" if self.prefix else ""
        names = sorted(
            name
            for name in raw
            if name.startswith(scope)
            and not is_excluded(name)
            and (matcher is None or matcher.match(PurePosixPath(name).name if any_depth else name[len(scope) :]))
        )
        return FileList(files=names[:limit], total=len(names), truncated=len(names) > limit)

    def _git_ls_files(self) -> list[str]:
        try:
            result = subprocess.run(
                [self.git_binary, "ls-files", "-z"],
                cwd=self.root,
                env=_child_env(),
                capture_output=True,
                timeout=self.list_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise CodeToolError("listing the files timed out") from None
        except OSError:
            raise CodeToolError("git could not be started (is it installed?)") from None
        if result.returncode != 0:
            raise CodeToolError("this folder is not a usable git checkout")
        return [name.decode("utf-8", errors="replace") for name in result.stdout.split(b"\x00") if name]

    # ----------------------------------------------------------------- search_code

    async def search_code(
        self,
        pattern: str,
        glob: str | None = None,
        *,
        limit: int = 20,
        context: int = 2,
        fixed_strings: bool = False,
    ) -> CodeSearchResult:
        """Matches of ``pattern`` (a regular expression, or plain text with ``fixed_strings``) under the scope."""
        if not pattern or len(pattern) > MAX_PATTERN_CHARS or "\x00" in pattern or "\n" in pattern:
            raise CodeToolError("pattern must be 1-200 characters on one line")
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        context = max(0, min(context, MAX_CONTEXT_LINES))

        command = [
            self.rg_binary, "--json", "--no-config", "--no-ignore", "--hidden", "--no-messages",
            "--max-filesize", "1M", "--max-columns", "300", "--max-columns-preview",
            "--max-count", str(MAX_MATCHES_PER_FILE), "--context", str(context), "--threads", "2",
        ]  # fmt: skip
        if fixed_strings:
            command.append("--fixed-strings")
        if glob:
            check_glob(glob)
            anchored = glob if "/" not in glob or not self.prefix else f"{self.prefix}/{glob}"  # same meaning as list_files
            command += ["--glob", anchored]
        for directory in sorted(EXCLUDED_DIRS):
            command += ["--glob", f"!**/{directory}/**"]
        for excluded in (*EXCLUDED_GLOBS, *SENSITIVE_GLOBS):
            command += ["--glob", f"!**/{excluded}"]
        command += ["-e", pattern, "--", self.prefix or "."]

        return await asyncio.to_thread(self._run_search, command, pattern, limit)

    def _run_search(self, command: list[str], pattern: str, limit: int) -> CodeSearchResult:
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=_child_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            raise CodeToolError("ripgrep (rg) could not be started (is it installed?)") from None

        timed_out = threading.Event()

        def stop() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(self.search_timeout_seconds, stop)
        timer.start()
        events: list[dict] = []
        matches, truncated = 0, False
        try:
            for line in process.stdout or ():
                event = json.loads(line)
                if event["type"] in ("match", "context"):
                    events.append(event["data"])
                    if event["type"] == "match":
                        matches += 1
                        if matches >= limit:
                            truncated = True
                            break
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            stderr = (process.stderr.read() if process.stderr else "").strip()
            process.wait()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()

        if timed_out.is_set():
            raise CodeToolError("the search timed out: use a more specific pattern or glob")
        if process.returncode == 2 and not events:
            detail = stderr.splitlines()[0][:ERROR_DETAIL_CHARS] if stderr else "unknown error"
            raise CodeToolError(f"search failed: {detail}")
        return CodeSearchResult(pattern=pattern, hits=_group(events), matches=matches, truncated=truncated)

    # ------------------------------------------------------------------- read_file

    async def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> FileSlice:
        """Lines ``start_line..end_line`` (at most 200) of one file, numbered. ``path`` is repository-relative."""
        return await asyncio.to_thread(self._read, path, start_line, end_line)

    def _read(self, raw_path: str, start_line: int, end_line: int | None) -> FileSlice:
        path = check_repo_path(raw_path)
        if is_sensitive(path):
            raise CodeToolError("this file is not available (it may hold secrets)")
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise CodeToolError("start_line must be 1 or more and end_line must not be before start_line")

        root = self.root.resolve()
        try:
            target = (root / path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise CodeToolError("file not found") from None
        if not target.is_relative_to(root):  # a symlink or junction that points outside the checkout
            raise CodeToolError("path leaves the repository")
        relative = target.relative_to(root)
        if any(part.lower() == ".git" for part in relative.parts) or is_sensitive(relative.as_posix()):
            raise CodeToolError("this file is not available")
        if not target.is_file():
            raise CodeToolError("not a file")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise CodeToolError("file is larger than 1 MB: search it instead of reading it")

        data = target.read_bytes()
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            raise CodeToolError("binary file")
        lines = data.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        if start_line > max(total, 1):
            raise CodeToolError(f"start_line is past the end of the file ({total} lines)")

        last = min(end_line if end_line is not None else start_line + MAX_READ_LINES - 1, start_line + MAX_READ_LINES - 1, total)
        chosen = lines[start_line - 1 : last]
        text = "\n".join(f"{number:>6} | {_clip(line)}" for number, line in enumerate(chosen, start=start_line))
        return FileSlice(
            path=relative.as_posix(),
            start_line=start_line,
            end_line=last if chosen else start_line - 1,
            total_lines=total,
            truncated=last < total,
            text=text,
        )


# ---------------------------------------------------------------- ripgrep's output


def _text_of(field: dict) -> str:
    """ripgrep gives ``{"text": ...}``, or ``{"bytes": base64}`` when the line is not valid UTF-8."""
    if "text" in field:
        return field["text"]
    return base64.b64decode(field.get("bytes", "")).decode("utf-8", errors="replace")


def _group(events: list[dict]) -> list[CodeHit]:
    """Merge match and context lines into runs of consecutive lines per file."""
    hits: list[CodeHit] = []
    current: CodeHit | None = None
    for data in events:
        path = _text_of(data["path"]).replace("\\", "/").removeprefix("./")
        number = data["line_number"]
        line = CodeLine(number=number, text=_clip(_text_of(data["lines"])), match=bool(data["submatches"]))
        if current is not None and current.path == path and number == current.last_line + 1:
            current.lines.append(line)
            current.last_line = number
        else:
            current = CodeHit(path=path, first_line=number, last_line=number, lines=[line])
            hits.append(current)
    return hits
