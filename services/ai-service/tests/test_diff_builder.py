"""The diff builder, checked by the only judge that matters: git itself (real git, temporary repositories)."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.diff import builder
from app.diff.builder import (
    build_checked_diff,
    build_diff,
    check_diff_applies,
    quote_path,
    split_lines,
)
from app.diff.validator import FileEdit, validate_changes
from app.schemas.domain import CodeChange
from tests.test_git_tools import _git

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.asyncio,
]

CODE = "import os\n\n\ndef charge(order):\n    response = gateway.call(request)\n    return response\n"


def edit(path: str, original: str | None, updated: str | None) -> FileEdit:
    return FileEdit(file_path=path, original=original, updated=updated, change_indices=[0])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed repository standing in for the snapshot of the pinned commit."""
    root = tmp_path / "snapshot"
    (root / "src").mkdir(parents=True)
    _git("init", "-b", "main", cwd=root)
    files = {
        "src/app.py": CODE,
        "src/other.py": "X = 1\n",
        "script.sh": "#!/bin/sh\necho hi\n",
        "dos.txt": "one\r\ntwo\r\nthree\r\n",
        "no_newline.txt": "alpha\nbeta",
    }
    for name, text in files.items():
        (root / name).write_bytes(text.encode("utf-8"))
    _git("add", "-A", cwd=root)
    _git("update-index", "--chmod=+x", "script.sh", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return root


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode() + path.read_bytes())
    return digest.hexdigest()


def apply_in_empty_folder(diff: str, originals: dict[str, bytes], tmp_path: Path) -> dict[str, bytes]:
    """Apply ``diff`` with a real ``git apply`` to a plain folder holding only ``originals`` (no repository),
    and return every file afterwards. This tests the builder independently of the check it feeds."""
    work = tmp_path / "roundtrip"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    for name, data in originals.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    result = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work, env=env, input=diff.encode("utf-8"), capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()
    return {p.relative_to(work).as_posix(): p.read_bytes() for p in work.rglob("*") if p.is_file()}


# ------------------------------------------------------------------------ the format


async def test_a_modify_is_a_git_style_unified_diff_that_applies(repo: Path, tmp_path: Path) -> None:
    updated = CODE.replace("gateway.call(request)", "gateway.call(request, timeout=5)")
    result = await build_checked_diff([edit("src/app.py", CODE, updated)], repo)

    assert result.applies and result.error is None
    lines = result.diff.splitlines()
    assert lines[0] == "diff --git a/src/app.py b/src/app.py" and lines[1:3] == ["--- a/src/app.py", "+++ b/src/app.py"]
    assert "-    response = gateway.call(request)" in lines and "+    response = gateway.call(request, timeout=5)" in lines
    assert any(line.startswith("@@") for line in lines)
    assert [(f.file_path, f.operation, f.additions, f.deletions) for f in result.files] == [("src/app.py", "modify", 1, 1)]
    assert apply_in_empty_folder(result.diff, {"src/app.py": CODE.encode()}, tmp_path) == {"src/app.py": updated.encode()}


async def test_an_add_is_a_new_file_diff(repo: Path, tmp_path: Path) -> None:
    result = await build_checked_diff([edit("src/retry.py", None, "RETRIES = 3\nBACKOFF = 2\n")], repo)
    assert result.applies
    assert "new file mode 100644" in result.diff and "--- /dev/null" in result.diff and "+++ b/src/retry.py" in result.diff
    assert (result.files[0].operation, result.files[0].additions, result.files[0].deletions) == ("add", 2, 0)
    assert apply_in_empty_folder(result.diff, {}, tmp_path) == {"src/retry.py": b"RETRIES = 3\nBACKOFF = 2\n"}


async def test_a_delete_uses_the_files_real_mode(repo: Path, tmp_path: Path) -> None:
    result = await build_checked_diff([edit("script.sh", "#!/bin/sh\necho hi\n", None), edit("src/other.py", "X = 1\n", None)], repo)
    assert result.applies
    assert "deleted file mode 100755" in result.diff and "deleted file mode 100644" in result.diff  # the executable one is not 100644
    assert "+++ /dev/null" in result.diff and {f.operation for f in result.files} == {"delete"}
    left = apply_in_empty_folder(result.diff, {"script.sh": b"#!/bin/sh\necho hi\n", "src/other.py": b"X = 1\n"}, tmp_path)
    assert left == {}


async def test_several_files_make_one_diff_in_path_order(repo: Path) -> None:
    edits = [edit("src/other.py", "X = 1\n", "X = 2\n"), edit("src/app.py", CODE, CODE + "# end\n")]
    result = await build_checked_diff(edits, repo)
    assert result.applies and [f.file_path for f in result.files] == ["src/app.py", "src/other.py"]
    assert result.diff.index("diff --git a/src/app.py") < result.diff.index("diff --git a/src/other.py")


async def test_far_apart_edits_become_separate_hunks_with_three_lines_of_context(repo: Path, tmp_path: Path) -> None:
    original = "".join(f"line {i}\n" for i in range(1, 61))
    updated = original.replace("line 5\n", "FIVE\n").replace("line 55\n", "FIFTY-FIVE\n")
    built = build_diff([edit("long.txt", original, updated)])
    assert built.diff.count("\n@@ ") == 2  # two hunks
    assert " line 2\n" in built.diff and " line 8\n" in built.diff and " line 1\n" not in built.diff  # three lines around the change
    assert apply_in_empty_folder(built.diff, {"long.txt": original.encode()}, tmp_path) == {"long.txt": updated.encode()}


# --------------------------------------------------------------------- awkward content


async def test_crlf_files_keep_their_line_endings_byte_for_byte(repo: Path, tmp_path: Path) -> None:
    result = await build_checked_diff([edit("dos.txt", "one\r\ntwo\r\nthree\r\n", "one\r\nTWO\r\nthree\r\n")], repo)
    assert result.applies and "-two\r\n" in result.diff and "+TWO\r\n" in result.diff
    out = apply_in_empty_folder(result.diff, {"dos.txt": b"one\r\ntwo\r\nthree\r\n"}, tmp_path)
    assert out == {"dos.txt": b"one\r\nTWO\r\nthree\r\n"}


async def test_a_missing_final_newline_is_marked_the_way_git_expects(repo: Path, tmp_path: Path) -> None:
    result = await build_checked_diff([edit("no_newline.txt", "alpha\nbeta", "alpha\nbeta\ngamma")], repo)
    assert result.applies and "\\ No newline at end of file" in result.diff
    assert apply_in_empty_folder(result.diff, {"no_newline.txt": b"alpha\nbeta"}, tmp_path) == {"no_newline.txt": b"alpha\nbeta\ngamma"}
    gained = await build_checked_diff([edit("no_newline.txt", "alpha\nbeta", "alpha\nbeta\n")], repo)  # only the newline changes
    assert gained.applies and apply_in_empty_folder(gained.diff, {"no_newline.txt": b"alpha\nbeta"}, tmp_path) == {"no_newline.txt": b"alpha\nbeta\n"}


async def test_lines_are_split_on_newline_only_so_odd_separators_do_not_corrupt_the_diff(repo: Path, tmp_path: Path) -> None:
    odd = "a\x0cb\nc d\ne\x1ff\n"  # form feed, line separator, unit separator inside lines
    new = odd.replace("e\x1ff", "E\x1ff")
    built = build_diff([edit("odd.txt", odd, new)])
    assert apply_in_empty_folder(built.diff, {"odd.txt": odd.encode()}, tmp_path) == {"odd.txt": new.encode()}
    assert split_lines("a\nb") == ["a\n", "b"] and split_lines("a\n") == ["a\n"] and split_lines("") == [] and split_lines("\n\n") == ["\n", "\n"]


async def test_content_that_looks_like_diff_syntax_is_just_content(repo: Path, tmp_path: Path) -> None:
    tricky = "--- a/fake\n+++ b/fake\n@@ -1 +1 @@\n-gone\n+here\n"
    updated = tricky + "diff --git a/x b/x\n"
    built = build_diff([edit("notes.txt", tricky, updated)])
    assert apply_in_empty_folder(built.diff, {"notes.txt": tricky.encode()}, tmp_path) == {"notes.txt": updated.encode()}


@pytest.mark.parametrize("path", ["docs/my notes.txt", 'quote".txt', "unicode/café.py", "plus+at@pct%.txt", "tab\there.txt"])
async def test_paths_with_spaces_quotes_and_unicode_are_quoted_the_way_git_reads_them(repo: Path, tmp_path: Path, path: str) -> None:
    target = repo / path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"one\n")  # bytes: write_text would turn the newline into CRLF on Windows
    except OSError:
        pytest.skip("this file system cannot hold that file name")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "path", cwd=repo)
    result = await build_checked_diff([edit(path, "one\n", "two\n")], repo)
    assert result.applies, result.error
    assert apply_in_empty_folder(result.diff, {path: b"one\n"}, tmp_path) == {path: b"two\n"}


def test_quote_path() -> None:
    assert quote_path("src/app.py") == "src/app.py" and quote_path("a+b@c%d-e_f.txt") == "a+b@c%d-e_f.txt"
    assert quote_path("my file.txt") == '"my file.txt"'
    assert quote_path('say "hi".txt') == '"say \\"hi\\".txt"' and quote_path("a\\b") == '"a\\\\b"'
    assert quote_path("café") == '"caf\\303\\251"' and quote_path("a\tb") == '"a\\011b"' and quote_path("") == '""'


# ----------------------------------------------------------------------- the check


async def test_a_diff_that_no_longer_matches_the_tree_is_reported_not_applied(repo: Path) -> None:
    updated = CODE.replace("response", "result")
    built = build_diff([edit("src/app.py", CODE, updated)])
    (repo / "src" / "app.py").write_text(CODE.replace("charge", "pay"), encoding="utf-8")  # the tree moved on
    before = tree_hash(repo)

    check = await check_diff_applies(built.diff, repo)

    assert not check.ok and check.error and "src/app.py" in check.error and len(check.error) <= 500
    assert tree_hash(repo) == before  # --check changed nothing


async def test_a_successful_check_changes_nothing_either(repo: Path) -> None:
    before = tree_hash(repo)
    result = await build_checked_diff([edit("src/other.py", "X = 1\n", "X = 2\n")], repo)
    assert result.applies and tree_hash(repo) == before


async def test_paths_that_climb_out_of_the_tree_are_refused_by_git_and_nothing_is_written(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    result = await build_checked_diff([edit("../outside.txt", None, "pwned\n")], repo)
    assert not result.applies and result.error and not outside.exists()
    absolute = await build_checked_diff([edit("/tmp/pwned.txt", None, "pwned\n")], repo)
    assert not absolute.applies


async def test_the_result_of_a_failed_check_still_carries_the_diff_for_the_retry(repo: Path) -> None:
    (repo / "src" / "app.py").write_text("something else\n", encoding="utf-8")
    result = await build_checked_diff([edit("src/app.py", CODE, CODE + "x\n")], repo)
    assert not result.applies and result.error and result.diff.startswith("diff --git") and result.files[0].additions == 1


async def test_an_empty_or_unchanging_set_of_edits_has_nothing_to_apply(repo: Path) -> None:
    assert not (await build_checked_diff([], repo)).applies
    unchanged = await build_checked_diff([edit("src/other.py", "X = 1\n", "X = 1\n")], repo)
    assert not unchanged.applies and unchanged.error == "there is nothing to apply: no change alters a file" and unchanged.files == []
    assert (await check_diff_applies("  \n", repo)).error == "there is nothing to apply: the diff is empty"
    assert build_diff([edit("a", "x\n", "x\n")]).files == []


async def test_a_huge_diff_is_refused_before_git_sees_it(repo: Path) -> None:
    result = await build_checked_diff([edit("big.txt", None, "x" * 600_000 + "\n")], repo)
    assert not result.applies and "larger than 500 KB" in (result.error or "")


async def test_git_problems_become_messages(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    built = build_diff([edit("src/other.py", "X = 1\n", "X = 2\n")])
    assert (await check_diff_applies(built.diff, repo, git_binary="git-does-not-exist")).error == "git could not be started (is it installed?)"

    def slow(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired("git", 20)

    monkeypatch.setattr(builder.subprocess, "run", slow)
    assert (await check_diff_applies(built.diff, repo)).error == "git apply --check timed out"


async def test_a_path_cannot_become_a_git_option(repo: Path) -> None:
    sneaky = edit("--unsafe-paths", None, "x\n")  # the diff goes in on stdin, the path is only text inside it
    built = build_diff([sneaky])
    assert built.diff.startswith("diff --git a/--unsafe-paths b/--unsafe-paths")
    assert (await check_diff_applies(built.diff, repo)).ok  # it is simply a file called --unsafe-paths


# ----------------------------------------------------------- validator -> builder -> git


async def test_from_a_proposed_change_to_a_diff_git_accepts(repo: Path, tmp_path: Path) -> None:
    changes = [
        CodeChange(file_path="src/app.py", operation="modify", explanation="timeout", old_code="    response = gateway.call(request)", new_code="    response = gateway.call(request, timeout=5)"),
        CodeChange(file_path="src/retry.py", operation="add", explanation="constant", new_code="RETRIES = 3"),
        CodeChange(file_path="src/other.py", operation="delete", explanation="unused"),
    ]
    report = validate_changes(repo, changes, read_paths={"src/app.py", "src/other.py"})
    assert report.ok

    result = await build_checked_diff(report.edits, repo)

    assert result.applies and [(f.file_path, f.operation) for f in result.files] == [("src/app.py", "modify"), ("src/other.py", "delete"), ("src/retry.py", "add")]
    out = apply_in_empty_folder(result.diff, {"src/app.py": CODE.encode(), "src/other.py": b"X = 1\n"}, tmp_path)
    assert out == {"src/app.py": CODE.replace("request)", "request, timeout=5)").encode(), "src/retry.py": b"RETRIES = 3\n"}
