"""The code tools (list, search, read) against a real throwaway git repository and real ripgrep."""

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app.tools import code
from app.tools.code import (
    CodeToolError,
    CodeTools,
    check_glob,
    check_repo_path,
    glob_to_regex,
    is_excluded,
    is_sensitive,
)

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
    pytest.mark.asyncio,
]

PAYMENT = """import logging

logger = logging.getLogger(__name__)


def charge(order):
    logger.info("charging %s", order)
    if order.total > 10_000:
        raise TimeoutError("gateway timeout")
    return True


def refund(order):
    return False
"""
SECRET = "SUPER_SECRET_VALUE_12345"


def _git(*args: str, cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }  # fmt: skip
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def _write(root: Path, name: str, text: str | bytes) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        target.write_bytes(text)
    else:
        target.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _write(root, "README.md", "# shop\nthe gateway timeout story\n")
    _write(root, "services/payment/app.py", PAYMENT)
    _write(root, "services/payment/test_app.py", "def test_charge():\n    assert True  # timeout\n")
    _write(root, "services/auth/login.py", "def login():\n    raise TimeoutError('auth timeout')\n")
    _write(root, "src/Main.java", "class Main {\n  void run() { throw new RuntimeException(\"timeout\"); }\n}\n")
    _write(root, ".github/workflows/ci.yml", "name: ci  # timeout-minutes: 5\n")
    _write(root, "node_modules/lib/index.js", "// timeout in vendored code\n")
    _write(root, "dist/bundle.js", "// timeout in a build artifact\n")
    _write(root, "static/app.min.js", "var timeout=1;\n")
    _write(root, ".env", f"API_KEY={SECRET}  # timeout\n")
    _write(root, "config/id_rsa", f"-----BEGIN KEY-----\n{SECRET}\n")
    _write(root, "certs/server.pem", f"{SECRET}\n")
    _write(root, "assets/logo.bin", b"\x89PNG\x00\x00timeout\x00binary")
    _write(root, "big.txt", "timeout\n" * 200_000)  # about 1.6 MB
    _write(root, "long.txt", "timeout " + "x" * 2000 + "\n")
    _git("add", "-A", "-f", cwd=root)  # -f: also the files .gitignore-style tools would skip
    _git("commit", "-m", "init", cwd=root)
    _write(root, "untracked.py", "timeout = 1\n")  # not committed
    return root


@pytest.fixture
def tools(repo: Path) -> CodeTools:
    return CodeTools(repo)


def scoped(repo: Path, prefix: str) -> CodeTools:
    return CodeTools(repo, prefix=prefix)


# ---------------------------------------------------------------------------- list_files


async def test_list_files_shows_tracked_files_only_without_noise_or_secrets(tools: CodeTools) -> None:
    result = await tools.list_files(limit=500)
    assert "services/payment/app.py" in result.files and "src/Main.java" in result.files
    assert ".github/workflows/ci.yml" in result.files  # a hidden folder is fine
    for hidden in ("node_modules/lib/index.js", "dist/bundle.js", "static/app.min.js", ".env", "config/id_rsa", "certs/server.pem", "untracked.py"):
        assert hidden not in result.files
    assert result.files == sorted(result.files) and not result.truncated


async def test_list_files_is_scoped_to_the_service_folder(repo: Path) -> None:
    result = await scoped(repo, "services/payment").list_files()
    assert result.files == ["services/payment/app.py", "services/payment/test_app.py"]  # paths stay repository-relative


async def test_list_files_glob_without_a_slash_matches_names_at_any_depth(tools: CodeTools) -> None:
    result = await tools.list_files("*.py", limit=500)
    assert result.files == ["services/auth/login.py", "services/payment/app.py", "services/payment/test_app.py"]


async def test_list_files_glob_with_a_slash_is_anchored_at_the_scope(repo: Path) -> None:
    assert (await CodeTools(repo).list_files("src/**/*.java")).files == ["src/Main.java"]
    assert (await CodeTools(repo).list_files("**/test_*.py")).files == ["services/payment/test_app.py"]
    assert (await scoped(repo, "services").list_files("payment/*.py")).files == ["services/payment/app.py", "services/payment/test_app.py"]
    assert (await CodeTools(repo).list_files("app.py")).files == ["services/payment/app.py"]


async def test_list_files_limit_truncates_and_reports_the_total(tools: CodeTools) -> None:
    result = await tools.list_files(limit=2)
    assert len(result.files) == 2 and result.truncated and result.total > 2
    assert (await tools.list_files(limit=0)).files  # a silly limit is clamped to 1, not an error


@pytest.mark.parametrize("glob", ["", ":(exclude)x", "!x", "../x", "/etc/*", "a\\b", "x" * 101, "a;b", "$(x)", "*.{py,java}", "[a-z].py", "*/*/*/*/*/*/*/x"])
async def test_a_glob_that_could_act_as_an_option_or_is_absurd_is_refused(tools: CodeTools, glob: str) -> None:
    if glob == "":
        assert (await tools.list_files(glob or None)).files  # empty means no filter
        return
    with pytest.raises(CodeToolError):
        await tools.list_files(glob)
    with pytest.raises(CodeToolError):
        await tools.search_code("timeout", glob)


async def test_a_folder_that_is_not_a_git_checkout_is_a_clean_error(tmp_path: Path) -> None:
    with pytest.raises(CodeToolError, match="git checkout"):
        await CodeTools(tmp_path).list_files()


# --------------------------------------------------------------------------- search_code


async def test_search_finds_matches_with_numbered_context_lines(tools: CodeTools) -> None:
    result = await tools.search_code("gateway timeout", "app.py", context=1)

    assert result.matches == 1 and not result.truncated
    (hit,) = result.hits
    assert hit.path == "services/payment/app.py"
    assert [(line.number, line.match) for line in hit.lines] == [(8, False), (9, True), (10, False)]
    assert hit.lines[1].text.strip() == 'raise TimeoutError("gateway timeout")'
    assert (hit.first_line, hit.last_line) == (8, 10)


async def test_search_context_zero_gives_just_the_lines(tools: CodeTools) -> None:
    result = await tools.search_code("TimeoutError", context=0)
    assert {h.path for h in result.hits} == {"services/payment/app.py", "services/auth/login.py"}
    assert all(len(h.lines) == 1 and h.lines[0].match for h in result.hits)


async def test_search_is_scoped_to_the_service_folder(repo: Path) -> None:
    result = await scoped(repo, "services/auth").search_code("timeout", context=0)
    assert [h.path for h in result.hits] == ["services/auth/login.py"]


async def test_search_regex_and_fixed_strings(tools: CodeTools) -> None:
    assert (await tools.search_code(r"raise \w+Error", context=0)).matches == 2
    assert (await tools.search_code("order.total > 10_000", fixed_strings=True, context=0)).matches == 1
    assert (await tools.search_code("a.b(", fixed_strings=True, context=0)).matches == 0  # "(" is just text here


async def test_search_never_looks_in_vendored_generated_secret_binary_or_untracked_places(tools: CodeTools) -> None:
    result = await tools.search_code("timeout", limit=50, context=0)
    paths = {h.path for h in result.hits}
    assert paths.isdisjoint({"node_modules/lib/index.js", "dist/bundle.js", "static/app.min.js", ".env", "config/id_rsa", "certs/server.pem", "assets/logo.bin"})
    assert not any(".git/" in p for p in paths)
    secret = await tools.search_code(SECRET, fixed_strings=True)
    assert secret.hits == [] and secret.matches == 0  # a secret in a committed file is not searchable
    assert (await tools.search_code("repositoryformatversion")).hits == []  # nothing from the .git folder


async def test_search_limits_results_and_says_so(tools: CodeTools) -> None:
    result = await tools.search_code("timeout", limit=2, context=0)
    assert result.matches == 2 and result.truncated
    assert (await tools.search_code("timeout", limit=1000, context=0)).matches <= code.MAX_SEARCH_LIMIT


async def test_search_takes_a_few_matches_per_file_and_skips_files_over_1mb(tools: CodeTools) -> None:
    result = await tools.search_code("timeout", "*.txt", limit=50, context=0)
    paths = [h.path for h in result.hits]
    assert "big.txt" not in paths  # over --max-filesize
    assert result.matches == 1 and paths == ["long.txt"]


async def test_long_lines_are_cut(tools: CodeTools) -> None:
    (hit,) = (await tools.search_code("timeout", "long.txt", context=0)).hits
    assert len(hit.lines[0].text) <= code.MAX_LINE_CHARS + 20


async def test_a_pattern_starting_with_a_dash_is_a_pattern_not_an_option(tools: CodeTools) -> None:
    assert (await tools.search_code("--files", fixed_strings=True)).matches == 0
    assert (await tools.search_code("-e foo")).matches == 0


@pytest.mark.parametrize("pattern", ["", "x" * 201, "a\nb", "a\x00b"])
async def test_unusable_patterns_are_refused(tools: CodeTools, pattern: str) -> None:
    with pytest.raises(CodeToolError, match="pattern"):
        await tools.search_code(pattern)


async def test_an_invalid_regex_is_a_clean_error(tools: CodeTools) -> None:
    with pytest.raises(CodeToolError, match="search failed"):
        await tools.search_code("(")


async def test_a_scope_that_does_not_exist_is_a_clean_error(repo: Path) -> None:
    with pytest.raises(CodeToolError, match="search failed"):
        await scoped(repo, "no/such/folder").search_code("timeout")


async def test_a_missing_ripgrep_is_explained(repo: Path) -> None:
    with pytest.raises(CodeToolError, match="installed"):
        await CodeTools(repo, rg_binary="rg-does-not-exist").search_code("timeout")


async def test_a_search_that_never_finishes_is_killed_and_reported(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killed = threading.Event()

    class Pipe:
        def __iter__(self):
            killed.wait(5)  # blocks until the watchdog kills the "process"
            return iter(())

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

    monkeypatch.setattr(code.subprocess, "Popen", lambda *a, **k: Hanging())
    with pytest.raises(CodeToolError, match="timed out"):
        await CodeTools(repo, search_timeout_seconds=0.1).search_code("timeout")


# ----------------------------------------------------------------------------- read_file


async def test_read_file_returns_numbered_lines(tools: CodeTools) -> None:
    result = await tools.read_file("services/payment/app.py", 6, 9)
    assert (result.path, result.start_line, result.end_line, result.total_lines, result.truncated) == ("services/payment/app.py", 6, 9, 14, True)
    assert result.text.splitlines()[0] == "     6 | def charge(order):"
    assert result.text.splitlines()[-1] == '     9 |         raise TimeoutError("gateway timeout")'


async def test_read_file_without_a_range_starts_at_the_top_and_reaches_the_end_of_a_short_file(tools: CodeTools) -> None:
    result = await tools.read_file("services/payment/app.py")
    assert (result.start_line, result.end_line, result.total_lines, result.truncated) == (1, 14, 14, False)


async def test_read_file_never_returns_more_than_200_lines(repo: Path) -> None:
    _write(repo, "long_file.py", "".join(f"line {i}\n" for i in range(1, 501)))
    result = await CodeTools(repo).read_file("long_file.py")
    assert (result.start_line, result.end_line, result.total_lines, result.truncated) == (1, 200, 500, True)
    assert len(result.text.splitlines()) == 200
    wider = await CodeTools(repo).read_file("long_file.py", 100, 900)  # asking for more is capped, not an error
    assert (wider.start_line, wider.end_line) == (100, 299)
    tail = await CodeTools(repo).read_file("long_file.py", 450, 500)
    assert (tail.end_line, tail.truncated) == (500, False)


async def test_read_file_cuts_long_lines(tools: CodeTools) -> None:
    result = await tools.read_file("long.txt")
    assert "[line cut]" in result.text and len(result.text) < 600


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "services/../../outside.txt", "/etc/passwd", "C:/Windows/win.ini", "c:\\x", "a\\b", "a\x00b", "a//b", "./README.md", "", "x" * 501, "services/./payment/app.py"],
)
async def test_read_file_refuses_paths_that_could_leave_the_checkout(tools: CodeTools, path: str) -> None:
    with pytest.raises(CodeToolError):
        await tools.read_file(path)


@pytest.mark.parametrize("path", [".git/config", ".git/HEAD", ".GIT/config", "services/.git/x"])
async def test_read_file_refuses_the_git_folder(tools: CodeTools, path: str) -> None:
    with pytest.raises(CodeToolError, match="git"):
        await tools.read_file(path)


@pytest.mark.parametrize("path", [".env", "config/id_rsa", "certs/server.pem"])
async def test_read_file_refuses_files_that_probably_hold_secrets(tools: CodeTools, path: str) -> None:
    with pytest.raises(CodeToolError, match="secrets"):
        await tools.read_file(path)


def _link_folder(link: Path, target: Path) -> None:
    """A folder link: a symlink where the system allows it, else a Windows junction (needs no privileges)."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
        if result.returncode == 0:
            return
    pytest.skip("this system does not allow creating folder links")


async def test_read_file_refuses_a_link_that_points_outside_the_checkout(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private", encoding="utf-8")
    _link_folder(repo / "linked", outside)
    with pytest.raises(CodeToolError, match="leaves the repository"):
        await CodeTools(repo).read_file("linked/private.txt")


async def test_read_file_errors(tools: CodeTools) -> None:
    for path, message in [
        ("nope.py", "not found"),
        ("services", "not a file"),
        ("assets/logo.bin", "binary"),
        ("big.txt", "larger than 1 MB"),
    ]:
        with pytest.raises(CodeToolError, match=message):
            await tools.read_file(path)
    with pytest.raises(CodeToolError, match="past the end"):
        await tools.read_file("services/payment/app.py", 99)
    with pytest.raises(CodeToolError, match="start_line"):
        await tools.read_file("services/payment/app.py", 0)
    with pytest.raises(CodeToolError, match="end_line"):
        await tools.read_file("services/payment/app.py", 5, 2)


async def test_read_file_can_read_outside_the_service_folder(repo: Path) -> None:
    # The service prefix is a default scope for list and search, not a wall: a stack trace can point into shared code.
    result = await scoped(repo, "services/payment").read_file("src/Main.java")
    assert result.path == "src/Main.java" and "class Main" in result.text


async def test_a_file_that_is_not_utf8_is_still_readable(repo: Path) -> None:
    _write(repo, "latin.txt", "caf\xe9 timeout\n".encode("latin-1"))
    result = await CodeTools(repo).read_file("latin.txt")
    assert "timeout" in result.text


# ----------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("glob", "yes", "no"),
    [
        ("*.py", ["a.py"], ["a/b.py", "a.pyc"]),
        ("**/*.py", ["a.py", "a/b.py", "a/b/c.py"], ["a.txt"]),
        ("src/**/*.java", ["src/A.java", "src/x/y/A.java"], ["lib/A.java"]),
        ("a?.txt", ["ab.txt"], ["abc.txt", "a/.txt"]),
        ("file.v1+x", ["file.v1+x"], ["fileXv1+x"]),  # regex characters in a glob are literal
    ],
)
async def test_glob_to_regex(glob: str, yes: list[str], no: list[str]) -> None:
    pattern = glob_to_regex(glob)
    assert all(pattern.match(p) for p in yes) and not any(pattern.match(p) for p in no)


async def test_the_exclusion_rules() -> None:
    assert all(is_excluded(p) for p in ["node_modules/x.js", "a/dist/b.js", "a/Vendor/x", "x.min.js", "uv.lock", "a/package-lock.json", ".env", "a/.env.local", "k/id_rsa", "c.pem", ".git/config"])
    assert not any(is_excluded(p) for p in ["src/app.py", "docs/environment.md", "services/payment/build_info.py", "README.md"])
    assert is_sensitive("a/b/.env.production") and not is_sensitive("envelope.py")


async def test_check_helpers_return_clean_values() -> None:
    assert check_repo_path("  src/a.py ") == "src/a.py"
    assert check_glob("src/**/*.java") == "src/**/*.java"
