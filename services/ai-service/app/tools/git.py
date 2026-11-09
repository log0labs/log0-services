"""Git history tools: what changed recently, who last touched these lines, what one commit did.

``GitTools`` works on the same checkout as ``CodeTools`` (3.5) and follows the same rules: only
repository-relative paths, nothing from ``.git`` itself, nothing that looks like a secret.
Three read-only tools:

* ``recent_commits(paths, since, until, limit)``: commits that touched some paths in a time
  window ("what changed before the incident?").
* ``blame(path, start_line, end_line)``: who last changed each line, in which commit and when.
* ``show_commit(sha)``: one commit's message, changed files and a bounded patch.

Honest about the clone being shallow (3.4 keeps only the last ``REPO_HISTORY_DEPTH`` commits): every
result says ``history_limited`` when older history may exist, and blame marks lines whose real origin
is no newer than the oldest commit the clone has as ``boundary`` (with ``history_limited`` that
means the real origin may be older still).

Safe by construction: arguments never reach git as options (commit ids must be hex, paths follow
``--`` with literal pathspecs, times are formatted from ``datetime`` objects, never from text), git
output is read through a size cap and a timeout, author e-mail addresses are never requested, and
patch sections for secret-bearing or vendored files are removed from ``show_commit``.
"""

import asyncio
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from app.tools.code import (
    CodeToolError,
    check_repo_path,
    child_env,
    is_excluded,
    is_sensitive,
    load_text_file,
)

logger = logging.getLogger(__name__)

MAX_COMMITS = 50
MAX_FILES_PER_COMMIT = 30
MAX_BLAME_LINES = 100
MAX_PATCH_LINES = 300
MAX_PATCH_CHARS = 20_000
MAX_MESSAGE_CHARS = 1000
MAX_SUBJECT_CHARS = 200
MAX_PATHS = 10
MAX_OUTPUT_BYTES = 2_000_000
MAX_PATCH_BYTES = 400_000
ERROR_DETAIL_CHARS = 200

_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_RECORD, _FIELD = "\x1e", "\x1f"


# --------------------------------------------------------------------------- results


class CommitSummary(BaseModel):
    sha: str
    short_sha: str
    author: str  # the name only: e-mail addresses are never read
    date: datetime
    subject: str
    files: list[str]  # changed files, secret-bearing and vendored ones left out, at most 30
    files_total: int


class CommitList(BaseModel):
    commits: list[CommitSummary]
    truncated: bool  # more commits matched than the limit
    history_limited: bool  # the clone is shallow: older commits may exist


class BlameLine(BaseModel):
    line: int
    sha: str
    short_sha: str
    author: str
    date: datetime
    summary: str
    boundary: bool  # the commit is the oldest available: if history_limited, the line may really be older
    text: str


class BlameResult(BaseModel):
    path: str
    start_line: int
    end_line: int
    lines: list[BlameLine]
    history_limited: bool


class FileChange(BaseModel):
    path: str
    added: int | None  # None for binary files
    deleted: int | None


class CommitDetail(BaseModel):
    sha: str
    short_sha: str
    author: str
    date: datetime
    message: str
    parents: int  # 2 or more: a merge commit (its patch is usually empty here)
    files: list[FileChange]
    omitted_files: int  # secret-bearing, vendored or beyond the file cap
    patch: str
    patch_truncated: bool
    history_limited: bool


def _short(sha: str) -> str:
    return sha[:8]


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.strip())


def _utc_iso(value: datetime) -> str:
    """``datetime`` -> an unambiguous UTC timestamp for ``--since``/``--until`` (naive means UTC)."""
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------ tools


@dataclass(frozen=True)
class GitTools:
    """History tools over one checkout. ``prefix`` is the service folder: the default for recent_commits."""

    root: Path
    prefix: str = ""
    git_binary: str = "git"
    timeout_seconds: float = 20.0

    # --------------------------------------------------------------- recent_commits

    async def recent_commits(
        self,
        paths: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        *,
        limit: int = 20,
    ) -> CommitList:
        """Newest first, merges left out. ``paths`` default to the service folder (or the whole repository)."""
        limit = max(1, min(limit, MAX_COMMITS))
        wanted = [check_repo_path(p) for p in paths[:MAX_PATHS]] if paths else ([self.prefix] if self.prefix else [])
        if any(is_sensitive(p) for p in wanted):
            raise CodeToolError("this file is not available (it may hold secrets)")

        command = [
            "log", "--no-merges", "--no-color", "--no-ext-diff", "--name-only",
            f"--max-count={limit + 1}", f"--format={_RECORD}%H{_FIELD}%aI{_FIELD}%an{_FIELD}%s",
        ]  # fmt: skip
        if since is not None:
            command.append(f"--since={_utc_iso(since)}")
        if until is not None:
            command.append(f"--until={_utc_iso(until)}")
        command += ["--", *wanted]

        output, _ = await asyncio.to_thread(self._git, command, MAX_OUTPUT_BYTES)
        commits = [self._parse_log_record(record) for record in output.split(_RECORD) if record.strip()]
        shallow = await asyncio.to_thread(self._is_shallow)
        return CommitList(commits=commits[:limit], truncated=len(commits) > limit, history_limited=shallow)

    @staticmethod
    def _parse_log_record(record: str) -> CommitSummary:
        header, _, rest = record.partition("\n")
        sha, date, author, subject = header.split(_FIELD, 3)
        names = [name for name in rest.splitlines() if name.strip()]
        visible = [name for name in names if not is_excluded(name)]
        return CommitSummary(
            sha=sha,
            short_sha=_short(sha),
            author=author,
            date=_when(date),
            subject=subject[:MAX_SUBJECT_CHARS],
            files=visible[:MAX_FILES_PER_COMMIT],
            files_total=len(visible),
        )

    # ------------------------------------------------------------------------ blame

    async def blame(self, path: str, start_line: int = 1, end_line: int | None = None) -> BlameResult:
        """Who last changed lines ``start_line..end_line`` (at most 100) of a file."""
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise CodeToolError("start_line must be 1 or more and end_line must not be before start_line")
        relative, data = await asyncio.to_thread(load_text_file, self.root, path)  # the same guards as read_file
        total = len(data.decode("utf-8", errors="replace").splitlines())
        if start_line > max(total, 1):
            raise CodeToolError(f"start_line is past the end of the file ({total} lines)")
        last = min(end_line if end_line is not None else start_line + MAX_BLAME_LINES - 1, start_line + MAX_BLAME_LINES - 1, total)

        output, _ = await asyncio.to_thread(
            self._git, ["blame", "--line-porcelain", "-w", "-L", f"{start_line},{last}", "--", relative], MAX_OUTPUT_BYTES
        )
        shallow = await asyncio.to_thread(self._is_shallow)
        return BlameResult(path=relative, start_line=start_line, end_line=last, lines=_parse_blame(output), history_limited=shallow)

    # ------------------------------------------------------------------ show_commit

    async def show_commit(self, sha: str) -> CommitDetail:
        """One commit: message, changed files with line counts, and a bounded patch."""
        sha = sha.strip()
        if not _SHA.match(sha):
            raise CodeToolError("sha must be 7 to 40 hexadecimal characters")
        kind, _ = await asyncio.to_thread(self._git, ["cat-file", "-t", sha], 1000, missing="commit not found in the available history")
        if kind.strip() != "commit":
            raise CodeToolError("that id is not a commit")

        meta, _ = await asyncio.to_thread(
            self._git,
            ["show", "-s", "--no-color", f"--format=%H{_FIELD}%aI{_FIELD}%an{_FIELD}%P{_FIELD}%B", sha],
            MAX_OUTPUT_BYTES,
        )
        full_sha, date, author, parents, message = meta.split(_FIELD, 4)
        numstat, _ = await asyncio.to_thread(self._git, ["show", "--no-color", "--format=", "--numstat", sha], MAX_OUTPUT_BYTES)
        raw_patch, cut = await asyncio.to_thread(
            self._git, ["show", "--no-color", "--no-ext-diff", "--no-textconv", "--format=", "--patch", sha], MAX_PATCH_BYTES
        )
        shallow = await asyncio.to_thread(self._is_shallow)

        changes = _parse_numstat(numstat)
        visible = [c for c in changes if not is_excluded(c.path)]
        patch, patch_truncated = _bounded_patch(_drop_hidden_sections(raw_patch), cut)
        return CommitDetail(
            sha=full_sha.strip(),
            short_sha=_short(full_sha.strip()),
            author=author,
            date=_when(date),
            message=message.strip()[:MAX_MESSAGE_CHARS],
            parents=len(parents.split()),
            files=visible[:MAX_FILES_PER_COMMIT],
            omitted_files=len(changes) - min(len(visible), MAX_FILES_PER_COMMIT),
            patch=patch,
            patch_truncated=patch_truncated,
            history_limited=shallow,
        )

    # -------------------------------------------------------------------- internals

    def _is_shallow(self) -> bool:
        output, _ = self._git(["rev-parse", "--is-shallow-repository"], 100)
        return output.strip() == "true"

    def _git(self, args: list[str], max_bytes: int, *, missing: str | None = None) -> tuple[str, bool]:
        """Run git, read at most ``max_bytes`` of output, kill it after the timeout. ``(text, was_cut)``."""
        env = {**child_env(), "GIT_LITERAL_PATHSPECS": "1"}  # a path is a path, never pathspec magic like ':(exclude)'
        try:
            process = subprocess.Popen(
                [self.git_binary, *args],
                cwd=self.root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            raise CodeToolError("git could not be started (is it installed?)") from None

        timed_out = threading.Event()

        def stop() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(self.timeout_seconds, stop)
        timer.start()
        cut = False
        try:
            assert process.stdout is not None
            chunks, size = [], 0
            while size <= max_bytes:
                chunk = process.stdout.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                cut, data = True, data[:max_bytes]
                process.kill()
        finally:
            timer.cancel()
            if process.poll() is None and not cut:
                process.wait()
            stderr = process.stderr.read().decode("utf-8", errors="replace").strip() if process.stderr else ""
            process.wait()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()

        if timed_out.is_set():
            raise CodeToolError("the git command timed out")
        if process.returncode != 0 and not cut:
            if missing is not None:
                raise CodeToolError(missing)
            detail = stderr.splitlines()[0][:ERROR_DETAIL_CHARS] if stderr else "unknown error"
            logger.info("git %s failed (%s)", args[0], process.returncode)
            raise CodeToolError(f"git {args[0]} failed: {detail}")
        return data.decode("utf-8", errors="replace"), cut


# ---------------------------------------------------------------------- git's output


def _parse_blame(output: str) -> list[BlameLine]:
    """``--line-porcelain``: for every line a header, ``key value`` lines, then the line itself after a TAB."""
    lines: list[BlameLine] = []
    header: list[str] = []
    fields: dict[str, str] = {}
    for raw in output.split("\n"):
        if raw.startswith("\t"):
            sha, _original, final = header[0], header[1], int(header[2])
            lines.append(
                BlameLine(
                    line=final,
                    sha=sha,
                    short_sha=_short(sha),
                    author=fields.get("author", "unknown"),
                    date=datetime.fromtimestamp(int(fields.get("author-time", "0")), tz=UTC),
                    summary=fields.get("summary", "")[:MAX_SUBJECT_CHARS],
                    boundary="boundary" in fields,
                    text=raw[1:][:400],
                )
            )
            header, fields = [], {}
        elif not header and re.match(r"^[0-9a-f]{40} \d+ \d+", raw):
            header = raw.split()
        elif header and raw:
            key, _, value = raw.partition(" ")
            fields[key] = value
    return lines


def _parse_numstat(output: str) -> list[FileChange]:
    changes: list[FileChange] = []
    for row in output.splitlines():
        parts = row.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        changes.append(
            FileChange(
                path=path,
                added=int(added) if added.isdigit() else None,  # "-" marks a binary file
                deleted=int(deleted) if deleted.isdigit() else None,
            )
        )
    return changes


_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def _drop_hidden_sections(patch: str) -> str:
    """Remove the parts of a patch that belong to secret-bearing or vendored files."""
    kept: list[str] = []
    skipping = False
    for line in patch.split("\n"):
        header = _DIFF_HEADER.match(line)
        if header:
            names = (header.group(1), header.group(2))
            skipping = any(is_excluded(PurePosixPath(name).as_posix()) for name in names)
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def _bounded_patch(patch: str, already_cut: bool) -> tuple[str, bool]:
    lines = patch.split("\n")
    truncated = already_cut or len(lines) > MAX_PATCH_LINES
    text = "\n".join(lines[:MAX_PATCH_LINES])
    if len(text) > MAX_PATCH_CHARS:
        text, truncated = text[:MAX_PATCH_CHARS], True
    return text, truncated
