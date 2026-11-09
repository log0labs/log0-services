"""The git tools (recent commits, blame, show a commit) against a real repository with real, dated history."""

import os
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.tools import git as git_module
from app.tools.code import CodeToolError
from app.tools.git import GitTools

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.asyncio,
]

SECRET = "SUPER_SECRET_VALUE_12345"
ALICE = ("Alice Example", "alice@example.com")
BOB = ("Bob Example", "bob@example.com")


def _git(*args: str, cwd: Path, who: tuple[str, str] = ALICE, when: str | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1],
        "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1],
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }  # fmt: skip
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit(root: Path, message: str, when: str, who: tuple[str, str] = ALICE, **files: str | bytes) -> str:
    for name, content in files.items():
        target = root / name.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    _git("add", "-A", "-f", cwd=root, who=who)
    _git("commit", "-m", message, cwd=root, who=who, when=when)
    return _git("rev-parse", "HEAD", cwd=root)


class History:
    """A repository with five dated commits (and one merge); the ids are kept for the assertions."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        _git("init", "-b", "main", cwd=root)
        self.c1 = _commit(root, "initial service", "2026-10-01T10:00:00+00:00", README="# shop\n", services__payment__app="line one\nline two\nline three\n")
        self.c2 = _commit(root, "payment: add retry", "2026-10-10T10:00:00+00:00", BOB, services__payment__app="line one\nline two changed by bob\nline three\nline four\n")
        self.c3 = _commit(
            root, "auth: add login and a local env file", "2026-10-20T10:00:00+00:00", ALICE,
            services__auth__login="def login():\n    pass\n", **{".env": f"API_KEY={SECRET}\n", "node_modules__lib__x.js": "// vendored\n"},
        )  # fmt: skip
        self.c4 = _commit(root, "assets: add a logo", "2026-10-22T10:00:00+00:00", BOB, assets__logo=b"\x89PNG\x00\x01\x02binary")
        self.c5 = _commit(root, "payment: huge generated table", "2026-10-25T10:00:00+00:00", ALICE, services__payment__table="".join(f"row {i}\n" for i in range(1, 801)))
        # a side branch merged with --no-ff: one merge commit with two parents
        _git("checkout", "-b", "feature", cwd=root)
        self.side = _commit(root, "feature: docs", "2026-10-26T10:00:00+00:00", BOB, docs__notes="notes\n")
        _git("checkout", "main", cwd=root)
        _git("merge", "--no-ff", "-m", "merge feature", "feature", cwd=root, when="2026-10-27T10:00:00+00:00")
        self.merge = _git("rev-parse", "HEAD", cwd=root)


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> History:
    """Built once for the whole file (about 30 git calls): the tests only read it."""
    return History(tmp_path_factory.mktemp("history") / "repo")


@pytest.fixture
def tools(history: History) -> GitTools:
    return GitTools(history.root)


# ------------------------------------------------------------------------ recent_commits


async def test_recent_commits_are_newest_first_without_merges(tools: GitTools, history: History) -> None:
    result = await tools.recent_commits()
    assert [c.sha for c in result.commits][:2] == [history.side, history.c5]
    subjects = [c.subject for c in result.commits]
    assert subjects == ["feature: docs", "payment: huge generated table", "assets: add a logo", "auth: add login and a local env file", "payment: add retry", "initial service"]
    assert history.merge not in [c.sha for c in result.commits]
    assert not result.truncated and not result.history_limited


async def test_a_commit_summary_has_the_author_date_and_short_id_but_never_an_email(tools: GitTools) -> None:
    result = await tools.recent_commits(limit=50)
    first = result.commits[-1]
    assert (first.author, first.short_sha, first.sha.startswith(first.short_sha)) == ("Alice Example", first.sha[:8], True)
    assert first.date == datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    assert "example.com" not in result.model_dump_json()  # no e-mail address anywhere


async def test_recent_commits_window_since_and_until(tools: GitTools) -> None:
    window = await tools.recent_commits(since=datetime(2026, 10, 9, tzinfo=UTC), until=datetime(2026, 10, 21, tzinfo=UTC))
    assert [c.subject for c in window.commits] == ["auth: add login and a local env file", "payment: add retry"]
    naive = await tools.recent_commits(since=datetime(2026, 10, 24), until=datetime(2026, 10, 25, 23))  # noqa: DTZ001 - naive means UTC
    assert [c.subject for c in naive.commits] == ["payment: huge generated table"]
    assert (await tools.recent_commits(since=datetime(2027, 1, 1, tzinfo=UTC))).commits == []


async def test_a_window_works_with_timezone_aware_values(tools: GitTools) -> None:
    until = datetime(2026, 10, 20, 15, 30, tzinfo=timezone(timedelta(hours=5)))  # 10:30 UTC
    result = await tools.recent_commits(since=until - timedelta(days=11), until=until)
    assert [c.subject for c in result.commits] == ["auth: add login and a local env file", "payment: add retry"]


async def test_recent_commits_filters_by_path_and_scope(history: History) -> None:
    all_tools = GitTools(history.root)
    payment = await all_tools.recent_commits(paths=["services/payment/app"])
    assert [c.subject for c in payment.commits] == ["payment: add retry", "initial service"]
    scoped = await GitTools(history.root, prefix="services/payment").recent_commits()  # the service folder is the default
    assert [c.subject for c in scoped.commits] == ["payment: huge generated table", "payment: add retry", "initial service"]
    explicit = await GitTools(history.root, prefix="services/payment").recent_commits(paths=["services/auth"])
    assert [c.subject for c in explicit.commits] == ["auth: add login and a local env file"]  # explicit paths win over the scope


async def test_recent_commits_limit_truncates(tools: GitTools) -> None:
    result = await tools.recent_commits(limit=2)
    assert len(result.commits) == 2 and result.truncated
    assert len((await tools.recent_commits(limit=500)).commits) == 6  # the cap is 50, there are only 6


async def test_changed_file_lists_hide_secret_and_vendored_files(tools: GitTools) -> None:
    result = await tools.recent_commits()
    auth = next(c for c in result.commits if c.subject.startswith("auth:"))
    assert auth.files == ["services/auth/login"] and auth.files_total == 1  # .env and node_modules are not shown
    assert SECRET not in result.model_dump_json() and ".env" not in result.model_dump_json()


async def test_a_secret_file_cannot_be_asked_about(tools: GitTools) -> None:
    with pytest.raises(CodeToolError, match="secrets"):
        await tools.recent_commits(paths=[".env"])


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "a\\b", "a\x00b", "x/../y", ".git/config", "C:/x", ":/"])
async def test_recent_commits_refuses_paths_that_leave_the_repository(tools: GitTools, path: str) -> None:
    with pytest.raises(CodeToolError):
        await tools.recent_commits(paths=[path])


async def test_a_path_that_looks_like_pathspec_magic_is_just_a_path_that_matches_nothing(tools: GitTools) -> None:
    # One at a time: git would read each as magic (everything except services / README, or files under services/payment).
    for magic in (":(exclude)services", ":!README", ":(top)services/payment", ":(glob)**/*"):
        result = await tools.recent_commits(paths=[magic])
        assert result.commits == [], magic  # literal names of files that do not exist


# --------------------------------------------------------------------------------- blame


async def test_blame_names_the_commit_author_and_date_of_each_line(tools: GitTools, history: History) -> None:
    result = await tools.blame("services/payment/app")

    assert (result.path, result.start_line, result.end_line) == ("services/payment/app", 1, 4)
    by_line = {line.line: line for line in result.lines}
    assert by_line[1].sha == history.c1 and by_line[1].author == "Alice Example" and by_line[1].summary == "initial service"
    assert by_line[2].sha == history.c2 and by_line[2].author == "Bob Example" and by_line[2].date == datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    assert by_line[2].text == "line two changed by bob" and by_line[4].sha == history.c2
    assert not result.history_limited
    assert [line.boundary for line in result.lines] == [True, False, True, False]  # lines 1 and 3 come from the root commit: the oldest there is
    assert "example.com" not in result.model_dump_json()


async def test_blame_range_and_cap(tools: GitTools) -> None:
    some = await tools.blame("services/payment/table", 10, 12)
    assert [line.line for line in some.lines] == [10, 11, 12] and some.lines[0].text == "row 10"
    default = await tools.blame("services/payment/table")  # 800 lines: the default and the cap are 100
    assert (default.start_line, default.end_line, len(default.lines)) == (1, 100, 100)
    wider = await tools.blame("services/payment/table", 50, 900)
    assert (wider.start_line, wider.end_line, len(wider.lines)) == (50, 149, 100)
    tail = await tools.blame("services/payment/table", 790, 5000)
    assert (tail.end_line, len(tail.lines)) == (800, 11)


async def test_blame_errors(tools: GitTools) -> None:
    for args, message in [
        (("nope.py",), "not found"),
        (("services",), "not a file"),
        (("assets/logo",), "binary"),
        (("services/payment/app", 99), "past the end"),
        (("services/payment/app", 0), "start_line"),
        (("services/payment/app", 4, 2), "end_line"),
    ]:
        with pytest.raises(CodeToolError, match=message):
            await tools.blame(*args)  # type: ignore[arg-type]


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", ".git/config", ".env", "a\\b", "a//b"])
async def test_blame_applies_the_same_path_guards_as_read_file(tools: GitTools, path: str) -> None:
    with pytest.raises(CodeToolError):
        await tools.blame(path)


async def test_a_file_that_is_not_tracked_cannot_be_blamed(tools: GitTools, history: History) -> None:
    scratch = history.root / "scratch.py"
    scratch.write_text("x = 1\n", encoding="utf-8")
    try:
        with pytest.raises(CodeToolError, match="blame failed"):
            await tools.blame("scratch.py")
    finally:
        scratch.unlink()  # the shared repository stays as it was


# ------------------------------------------------------------------------- show_commit


async def test_show_commit_gives_message_author_files_and_patch(tools: GitTools, history: History) -> None:
    detail = await tools.show_commit(history.c2)

    assert (detail.sha, detail.short_sha, detail.author, detail.parents) == (history.c2, history.c2[:8], "Bob Example", 1)
    assert detail.message == "payment: add retry" and detail.date == datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    assert [(f.path, f.added, f.deleted) for f in detail.files] == [("services/payment/app", 2, 1)]
    assert "+line two changed by bob" in detail.patch and "-line two" in detail.patch
    assert not detail.patch_truncated and detail.omitted_files == 0 and not detail.history_limited
    assert "example.com" not in detail.model_dump_json()


async def test_a_short_or_upper_case_id_works(tools: GitTools, history: History) -> None:
    assert (await tools.show_commit(history.c2[:7])).sha == history.c2
    assert (await tools.show_commit(history.c2[:12].upper())).sha == history.c2


async def test_show_commit_leaves_out_secret_and_vendored_files_from_files_and_patch(tools: GitTools, history: History) -> None:
    detail = await tools.show_commit(history.c3)
    assert [f.path for f in detail.files] == ["services/auth/login"]
    assert detail.omitted_files == 2  # .env and node_modules/lib/x.js
    assert SECRET not in detail.patch and "API_KEY" not in detail.patch and "vendored" not in detail.patch
    assert "def login()" in detail.patch and ".env" not in detail.patch and "node_modules" not in detail.patch


async def test_a_huge_patch_is_cut_and_says_so(tools: GitTools, history: History) -> None:
    detail = await tools.show_commit(history.c5)
    assert detail.patch_truncated and len(detail.patch.splitlines()) <= 300 and len(detail.patch) <= 20_000
    assert detail.files[0].path == "services/payment/table" and detail.files[0].added == 800


async def test_a_binary_change_has_no_line_counts_and_no_content(tools: GitTools, history: History) -> None:
    detail = await tools.show_commit(history.c4)
    assert [(f.path, f.added, f.deleted) for f in detail.files] == [("assets/logo", None, None)]
    assert "PNG" not in detail.patch


async def test_a_merge_commit_reports_its_parents(tools: GitTools, history: History) -> None:
    detail = await tools.show_commit(history.merge)
    assert detail.parents == 2 and detail.message == "merge feature"


@pytest.mark.parametrize("sha", ["", "abc", "zzzzzzzz", "--help", "-p", "HEAD", "main", "HEAD~1", "a" * 41, "abc; rm -rf", "1234567 --stat", "../x"])
async def test_only_hexadecimal_ids_are_accepted_so_nothing_can_be_an_option_or_a_ref(tools: GitTools, sha: str) -> None:
    with pytest.raises(CodeToolError, match="hexadecimal"):
        await tools.show_commit(sha)


async def test_unknown_ids_and_non_commits_are_clean_errors(tools: GitTools, history: History) -> None:
    with pytest.raises(CodeToolError, match="not found"):
        await tools.show_commit("deadbeef" * 5)
    tree = _git("rev-parse", "HEAD^{tree}", cwd=history.root)
    with pytest.raises(CodeToolError, match="not a commit"):
        await tools.show_commit(tree)


# ------------------------------------------------------------------- a shallow checkout


@pytest.fixture
def shallow(history: History, tmp_path: Path) -> GitTools:
    clone = tmp_path / "shallow"
    _git("clone", "--depth", "3", "--no-local", history.root.as_uri(), str(clone), cwd=tmp_path)
    return GitTools(clone)


async def test_a_shallow_clone_says_its_history_is_limited(shallow: GitTools) -> None:
    result = await shallow.recent_commits()
    assert result.history_limited
    assert len(result.commits) <= 3


async def test_blame_marks_lines_whose_true_origin_is_older_than_the_clone(shallow: GitTools, history: History) -> None:
    result = await shallow.blame("services/payment/app")
    assert result.history_limited
    first = next(line for line in result.lines if line.line == 1)  # really from c1, which the clone does not have
    assert first.boundary and first.sha != history.c1


async def test_a_commit_outside_the_available_history_is_reported_as_missing(shallow: GitTools, history: History) -> None:
    with pytest.raises(CodeToolError, match="not found in the available history"):
        await shallow.show_commit(history.c1)
    assert (await shallow.show_commit(history.merge)).history_limited


# ------------------------------------------------------------------------ git itself


async def test_a_folder_that_is_not_a_repository_is_a_clean_error(tmp_path: Path) -> None:
    with pytest.raises(CodeToolError, match="failed"):
        await GitTools(tmp_path).recent_commits()


async def test_a_missing_git_binary_is_explained(history: History) -> None:
    with pytest.raises(CodeToolError, match="installed"):
        await GitTools(history.root, git_binary="git-does-not-exist").recent_commits()


async def test_output_is_read_through_a_size_cap(tools: GitTools) -> None:
    text, cut = tools._git(["log", "--format=%H"], 50)
    assert cut and len(text.encode()) == 50
    text, cut = tools._git(["log", "--format=%H"], 10_000)
    assert not cut and text.count("\n") >= 6


async def test_a_git_that_never_answers_is_killed_and_reported(history: History, monkeypatch: pytest.MonkeyPatch) -> None:
    killed = threading.Event()

    class Pipe:
        def read(self, _size: int = -1) -> bytes:
            killed.wait(5)  # blocks until the watchdog kills the "process"
            return b""

        def close(self) -> None: ...

    class Hanging:
        returncode = None
        stdout = Pipe()
        stderr = None

        def kill(self) -> None:
            killed.set()
            self.returncode = -9

        def poll(self):
            return self.returncode

        def wait(self) -> None: ...

    monkeypatch.setattr(git_module.subprocess, "Popen", lambda *a, **k: Hanging())
    with pytest.raises(CodeToolError, match="timed out"):
        await GitTools(history.root, timeout_seconds=0.1).recent_commits()
