"""The six code and git tools as a model sees them: a name, a description, typed arguments, a
bounded text result.

``CodeTools`` and ``GitTools`` (3.5, 3.6) return typed objects. A model cannot read those; it reads
text. This module is the thin layer between: it declares each tool's arguments (the JSON schema the
model is shown, with a description for every parameter), runs the real tool, and renders the result
as compact text. It also reports which files and commits the result *mentioned*, so the
investigator can later check that a finding cites only things a tool really returned.

Nothing here is security-critical by itself: the guards live in the tools. A model that sends bad
arguments gets an error message back (the investigator turns ``CodeToolError`` and validation errors
into tool messages), never an exception.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.tools.code import CodeSearchResult, CodeTools, FileList, FileSlice
from app.tools.git import BlameResult, CommitDetail, CommitList, GitTools

MAX_RESULT_CHARS = 6000
CUT_MARK = "\n...[result cut]"


@dataclass(frozen=True)
class ToolResult:
    text: str
    paths: tuple[str, ...] = ()  # repository paths the result mentioned
    shas: tuple[str, ...] = ()  # full commit ids the result mentioned


def clip(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[: MAX_RESULT_CHARS - len(CUT_MARK)] + CUT_MARK


# ------------------------------------------------------------------------- arguments


class ListFilesArgs(BaseModel):
    glob: str | None = Field(
        default=None,
        description="Optional file name pattern: '*.py' matches names at any depth, 'src/**/*.java' is anchored "
        "at the service folder. Letters, digits and . _ - / * ? only.",
    )
    limit: int = Field(default=100, description="How many paths to return (at most 500).")


class SearchCodeArgs(BaseModel):
    pattern: str = Field(description="What to look for: a regular expression, or plain text with fixed_strings. One line, at most 200 characters.")
    glob: str | None = Field(default=None, description="Optional file name pattern to limit the search, like '*.py'.")
    limit: int = Field(default=20, description="How many matches to return (at most 50).")
    context: int = Field(default=2, description="Lines of context around each match (0 to 5).")
    fixed_strings: bool = Field(default=False, description="True to treat the pattern as plain text, not a regular expression.")


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path relative to the repository root, with forward slashes, as returned by the other tools.")
    start_line: int = Field(default=1, description="First line to read (1 is the top of the file).")
    end_line: int | None = Field(default=None, description="Last line to read; at most 200 lines are returned per call.")


class RecentCommitsArgs(BaseModel):
    paths: list[str] | None = Field(default=None, description="Only commits touching these files or folders. Default: the service folder.")
    since: datetime | None = Field(default=None, description="Only commits after this time, ISO 8601 like 2026-01-01T12:00:00Z.")
    until: datetime | None = Field(default=None, description="Only commits before this time, ISO 8601.")
    limit: int = Field(default=20, description="How many commits to return (at most 50).")


class BlameArgs(BaseModel):
    path: str = Field(description="File to blame, relative to the repository root.")
    start_line: int = Field(default=1, description="First line.")
    end_line: int | None = Field(default=None, description="Last line; at most 100 lines are returned.")


class ShowCommitArgs(BaseModel):
    sha: str = Field(description="Commit id, 7 to 40 hexadecimal characters, as returned by recent_commits or blame.")


# --------------------------------------------------------------------------- rendering


def render_file_list(result: FileList) -> ToolResult:
    head = f"{len(result.files)} of {result.total} files" + (" (more exist: narrow it with a glob)" if result.truncated else "")
    return ToolResult(clip("\n".join([head, *result.files])), paths=tuple(result.files))


def render_search(result: CodeSearchResult) -> ToolResult:
    if not result.hits:
        return ToolResult(f"No matches for {result.pattern!r}.")
    parts = []
    for hit in result.hits:
        parts.append(f"{hit.path} lines {hit.first_line}-{hit.last_line}")
        parts += [f"{line.number:>6}{'>' if line.match else ' '}| {line.text}" for line in hit.lines]
    if result.truncated:
        parts.append("(stopped at the match limit: narrow the pattern or add a glob)")
    return ToolResult(clip("\n".join(parts)), paths=tuple(dict.fromkeys(hit.path for hit in result.hits)))


def render_slice(result: FileSlice) -> ToolResult:
    head = f"{result.path} lines {result.start_line}-{result.end_line} of {result.total_lines}"
    tail = f"\n(more lines follow: ask for start_line={result.end_line + 1})" if result.truncated else ""
    return ToolResult(clip(f"{head}\n{result.text}{tail}"), paths=(result.path,))


def render_commits(result: CommitList) -> ToolResult:
    if not result.commits:
        return ToolResult("No commits matched." + (" (the history available here is limited to recent commits)" if result.history_limited else ""))
    parts, paths = [], []
    for commit in result.commits:
        parts.append(f"{commit.short_sha} {commit.date:%Y-%m-%d %H:%M}Z {commit.author}: {commit.subject}")
        if commit.files:
            more = f" (+{commit.files_total - len(commit.files)} more)" if commit.files_total > len(commit.files) else ""
            parts.append(f"        files: {', '.join(commit.files)}{more}")
            paths += commit.files
    if result.truncated:
        parts.append("(more commits exist: narrow the paths or the time window)")
    if result.history_limited:
        parts.append("(the history available here is limited to recent commits)")
    return ToolResult(clip("\n".join(parts)), paths=tuple(dict.fromkeys(paths)), shas=tuple(c.sha for c in result.commits))


def render_blame(result: BlameResult) -> ToolResult:
    parts = [f"{result.path} lines {result.start_line}-{result.end_line}"]
    for line in result.lines:
        mark = "^" if line.boundary else " "
        parts.append(f"{line.line:>6} {mark}{line.short_sha} {line.author} {line.date:%Y-%m-%d} | {line.text}")
    if result.history_limited:
        parts.append("(^ marks the oldest commit available here: the real origin of that line may be older)")
    return ToolResult(clip("\n".join(parts)), paths=(result.path,), shas=tuple(dict.fromkeys(line.sha for line in result.lines)))


def render_commit(result: CommitDetail) -> ToolResult:
    files = [f"  {c.path} (+{c.added if c.added is not None else '?'} -{c.deleted if c.deleted is not None else '?'})" for c in result.files]
    omitted = f"\n  ({result.omitted_files} more files not shown)" if result.omitted_files else ""
    cut = "\n(patch cut)" if result.patch_truncated else ""
    text = (
        f"commit {result.sha}\n{result.author}, {result.date:%Y-%m-%d %H:%M}Z, parents: {result.parents}\n\n"
        f"{result.message}\n\nfiles:\n" + "\n".join(files) + omitted + f"\n\n{result.patch}{cut}"
    )
    return ToolResult(clip(text), paths=tuple(c.path for c in result.files), shas=(result.sha,))


# ----------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[BaseModel], Awaitable[ToolResult]]

    def as_langchain_tool(self) -> StructuredTool:
        """The schema form that ``bind_tools`` shows the model. It is never called by LangChain: the
        investigator runs ``run`` itself, so it can count, time-box and sanitise every call."""

        async def _unused(**_: object) -> str:  # pragma: no cover - never invoked
            raise RuntimeError("the investigator runs tools itself")

        return StructuredTool.from_function(coroutine=_unused, name=self.name, description=self.description, args_schema=self.args)


def build_toolset(code: CodeTools, git: GitTools) -> dict[str, ToolSpec]:
    """The six tools, keyed by name, over one checkout."""

    async def list_files(a: ListFilesArgs) -> ToolResult:
        return render_file_list(await code.list_files(a.glob, limit=a.limit))

    async def search_code(a: SearchCodeArgs) -> ToolResult:
        return render_search(await code.search_code(a.pattern, a.glob, limit=a.limit, context=a.context, fixed_strings=a.fixed_strings))

    async def read_file(a: ReadFileArgs) -> ToolResult:
        return render_slice(await code.read_file(a.path, a.start_line, a.end_line))

    async def recent_commits(a: RecentCommitsArgs) -> ToolResult:
        return render_commits(await git.recent_commits(a.paths, a.since, a.until, limit=a.limit))

    async def blame(a: BlameArgs) -> ToolResult:
        return render_blame(await git.blame(a.path, a.start_line, a.end_line))

    async def show_commit(a: ShowCommitArgs) -> ToolResult:
        return render_commit(await git.show_commit(a.sha))

    specs = [
        ToolSpec("list_files", "List the files of the repository (or the service folder), optionally filtered by a name pattern. Use it to find where something lives.", ListFilesArgs, list_files),
        ToolSpec("search_code", "Search the code for text or a regular expression. Returns matching lines with numbered context. Use it to find where a symbol, message or constant appears.", SearchCodeArgs, search_code),
        ToolSpec("read_file", "Read lines of one file, numbered. At most 200 lines per call: ask for the next range to continue.", ReadFileArgs, read_file),
        ToolSpec("recent_commits", "List recent commits (newest first), optionally only those touching some paths or inside a time window. Use it to see what changed before the incident.", RecentCommitsArgs, recent_commits),
        ToolSpec("blame", "Show who last changed each line of a file range, in which commit and when.", BlameArgs, blame),
        ToolSpec("show_commit", "Show one commit: message, changed files and the patch.", ShowCommitArgs, show_commit),
    ]  # fmt: skip
    return {spec.name: spec for spec in specs}
