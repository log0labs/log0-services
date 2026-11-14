"""Turn validated edits into a unified diff, and prove git accepts it.

The validator (4.3) produces ``FileEdit`` objects: a file as it is and as it would be. This module
writes them as **one git-style unified diff** and then asks git itself whether it applies:

* ``build_diff`` uses ``difflib`` (never the model: a model's own diff is very often malformed) and
  writes what ``git apply`` expects: ``diff --git`` headers, ``new file mode`` / ``deleted file
  mode`` for adds and deletes, hunks with three lines of context, the ``\\ No newline at end of
  file`` marker, CRLF lines untouched, and C-style quoting for paths with spaces or non-ASCII
  characters. Lines are split on ``\\n`` only, like git does (``str.splitlines`` would also split on
  form feeds and Unicode separators and corrupt the diff).
* ``check_diff_applies`` runs ``git apply --check`` in the snapshot of the pinned commit: it changes
  nothing, it only says whether the patch would apply cleanly to that exact tree.
* ``build_checked_diff`` does both and returns one result.

A diff that fails the check is **not** discarded or repaired: ``applies`` is ``False`` with git's own
reason, so the retry in step 4.5 (and the engineer) can see what went wrong. The untrusted parts, the
paths and the text, only ever reach git as data on stdin or as quoted header names, never as options.
"""

import asyncio
import difflib
import logging
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.diff.validator import FileEdit
from app.tools.code import child_env

logger = logging.getLogger(__name__)

CONTEXT_LINES = 3
MAX_DIFF_BYTES = 500_000
MAX_ERROR_CHARS = 500
APPLY_TIMEOUT_SECONDS = 20.0
_SAFE_PATH_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./+@%-")
_NO_NEWLINE = "\\ No newline at end of file\n"


class FileDiff(BaseModel):
    file_path: str
    operation: Literal["add", "modify", "delete"]
    additions: int
    deletions: int


class BuiltDiff(BaseModel):
    diff: str
    files: list[FileDiff]


class ApplyCheck(BaseModel):
    ok: bool
    error: str | None = None  # git's own words, short


class DiffResult(BaseModel):
    diff: str
    files: list[FileDiff]
    applies: bool
    error: str | None = None


# --------------------------------------------------------------------------- building


def split_lines(text: str) -> list[str]:
    """Lines with their ``\\n`` kept, splitting on ``\\n`` only (as git does). The last line has no
    ``\\n`` when the text does not end with one."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def quote_path(path: str) -> str:
    """The path as git writes it in a diff header: bare when plain, C-quoted otherwise."""
    if path and all(char in _SAFE_PATH_CHARS for char in path):
        return path
    escaped = []
    for byte in path.encode("utf-8"):
        char = chr(byte)
        if char == '"':
            escaped.append('\\"')
        elif char == "\\":
            escaped.append("\\\\")
        elif 0x20 <= byte < 0x7F:
            escaped.append(char)
        else:
            escaped.append(f"\\{byte:03o}")
    return '"' + "".join(escaped) + '"'


def _terminate(line: str) -> str:
    """A diff line must end with a newline; one that does not carries git's "no newline" marker."""
    return line if line.endswith("\n") else line + "\n" + _NO_NEWLINE


def _file_diff(edit: FileEdit, mode: str) -> tuple[str, FileDiff]:
    before = split_lines(edit.original or "")
    after = split_lines(edit.updated or "")
    operation: Literal["add", "modify", "delete"] = "add" if edit.original is None else "delete" if edit.updated is None else "modify"
    old_name = "/dev/null" if operation == "add" else quote_path(f"a/{edit.file_path}")
    new_name = "/dev/null" if operation == "delete" else quote_path(f"b/{edit.file_path}")

    header = [f"diff --git {quote_path('a/' + edit.file_path)} {quote_path('b/' + edit.file_path)}\n"]
    if operation == "add":
        header.append(f"new file mode {mode}\n")
    elif operation == "delete":
        header.append(f"deleted file mode {mode}\n")

    body = list(difflib.unified_diff(before, after, fromfile=old_name, tofile=new_name, n=CONTEXT_LINES, lineterm="\n"))
    additions = sum(1 for line in body if line.startswith("+") and not line.startswith("+++ "))
    deletions = sum(1 for line in body if line.startswith("-") and not line.startswith("--- "))
    text = "".join(header) + "".join(_terminate(line) for line in body)
    return text, FileDiff(file_path=edit.file_path, operation=operation, additions=additions, deletions=deletions)


def build_diff(edits: Iterable[FileEdit], *, modes: dict[str, str] | None = None) -> BuiltDiff:
    """One unified diff for all ``edits``, files in path order. ``modes`` gives the git file mode of
    existing files (needed for deletes; ``100644`` when unknown). Edits that change nothing are skipped."""
    pieces: list[str] = []
    files: list[FileDiff] = []
    for edit in sorted(edits, key=lambda e: e.file_path):
        if edit.original == edit.updated:
            continue
        text, summary = _file_diff(edit, (modes or {}).get(edit.file_path, "100644"))
        pieces.append(text)
        files.append(summary)
    return BuiltDiff(diff="".join(pieces), files=files)


# ---------------------------------------------------------------------------- checking


def _run_git(git: str, args: list[str], root: Path, *, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [git, *args],
        cwd=root,
        env=child_env(),
        input=data,
        capture_output=True,
        timeout=APPLY_TIMEOUT_SECONDS,
        check=False,
    )


def _file_modes(git: str, root: Path, paths: list[str]) -> dict[str, str]:
    """The git mode (``100644`` or ``100755``) of existing files, from the index of the snapshot."""
    modes: dict[str, str] = {}
    for path in paths:
        try:
            result = _run_git(git, ["ls-files", "-s", "--", path], root)
        except (subprocess.TimeoutExpired, OSError):
            continue
        fields = result.stdout.decode("utf-8", errors="replace").split()
        if result.returncode == 0 and fields:
            modes[path] = fields[0]
    return modes


def _check(git: str, root: Path, diff: str) -> ApplyCheck:
    if not diff.strip():
        return ApplyCheck(ok=False, error="there is nothing to apply: the diff is empty")
    data = diff.encode("utf-8")
    if len(data) > MAX_DIFF_BYTES:
        return ApplyCheck(ok=False, error=f"the diff is larger than {MAX_DIFF_BYTES // 1000} KB")
    try:
        result = _run_git(git, ["apply", "--check", "--whitespace=nowarn", "-"], root, data=data)
    except subprocess.TimeoutExpired:
        return ApplyCheck(ok=False, error="git apply --check timed out")
    except OSError:
        return ApplyCheck(ok=False, error="git could not be started (is it installed?)")
    if result.returncode == 0:
        return ApplyCheck(ok=True)
    detail = " ".join(result.stderr.decode("utf-8", errors="replace").split())[:MAX_ERROR_CHARS]
    return ApplyCheck(ok=False, error=detail or f"git apply --check failed ({result.returncode})")


async def check_diff_applies(diff: str, root: Path, *, git_binary: str = "git") -> ApplyCheck:
    """Would ``diff`` apply cleanly to the tree at ``root`` (the snapshot of the pinned commit)?

    ``git apply --check`` changes nothing. It refuses paths outside ``root`` by itself (we do not pass
    ``--unsafe-paths``), and the diff text reaches git on stdin, so nothing in it can act as an option.
    """
    return await asyncio.to_thread(_check, git_binary, root, diff)


async def build_checked_diff(edits: list[FileEdit], root: Path, *, git_binary: str = "git") -> DiffResult:
    """Build the diff for ``edits`` and check it against the snapshot at ``root``."""
    deleted = [e.file_path for e in edits if e.updated is None]
    modes = await asyncio.to_thread(_file_modes, git_binary, root, deleted) if deleted else {}
    built = build_diff(edits, modes=modes)
    if not built.files:
        return DiffResult(diff="", files=[], applies=False, error="there is nothing to apply: no change alters a file")
    check = await check_diff_applies(built.diff, root, git_binary=git_binary)
    if not check.ok:
        logger.info("a built diff failed git apply --check")
    return DiffResult(diff=built.diff, files=built.files, applies=check.ok, error=check.error)
