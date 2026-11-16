"""The change validator, against a plain folder (no git, no network): every way a change can be wrong."""

import os
import subprocess
from pathlib import Path

import pytest

from app.diff.validator import (
    ChangeIssue,
    IssueCode,
    ValidationReport,
    find_all,
    validate_changes,
    whitespace_matches,
)
from app.schemas.domain import CodeChange

APP = "services/payment/app.py"
CODE = (
    "import logging\n"
    "\n"
    "def charge(order):\n"
    "    logger.info('charging')\n"
    "    response = gateway.call(request)  # no timeout set\n"
    "    return response\n"
    "\n"
    "def refund(order):\n"
    "    logger.info('charging')\n"
    "    return None\n"
)
OLD = "    response = gateway.call(request)  # no timeout set"
NEW = "    response = gateway.call(request, timeout=5)"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "services" / "payment").mkdir(parents=True)
    (tmp_path / APP).write_text(CODE, encoding="utf-8", newline="")
    (tmp_path / "services" / "payment" / "util.py").write_text("X = 1\n", encoding="utf-8", newline="")
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    return tmp_path


def modify(old: str = OLD, new: str = NEW, path: str = APP) -> CodeChange:
    return CodeChange(file_path=path, operation="modify", explanation="why", old_code=old, new_code=new)


def issue_codes(report: ValidationReport) -> list[IssueCode]:
    return [i.code for i in report.issues]


# ---------------------------------------------------------------------------- success


def test_a_matching_modify_becomes_the_new_content_of_the_file(root: Path) -> None:
    report = validate_changes(root, [modify()], read_paths={APP})

    assert report.ok and report.issues == [] and report.valid_indices == [0] and report.failed_indices == []
    (edit,) = report.edits
    assert (edit.file_path, edit.original) == (APP, CODE) and edit.change_indices == [0]
    assert edit.updated == CODE.replace(OLD, NEW) and "timeout=5" in edit.updated
    assert (root / APP).read_text(encoding="utf-8") == CODE  # the validator never touches the repository


def test_two_separate_edits_to_one_file_are_both_applied_to_the_original_text(root: Path) -> None:
    first = modify(old="    return response", new="    return response.json()")
    second = modify(old="def refund(order):\n    logger.info('charging')", new="def refund(order):\n    logger.info('refunding')")
    report = validate_changes(root, [second, first], read_paths={APP})  # given out of order: positions decide
    assert report.ok
    updated = report.edits[0].updated or ""
    assert "return response.json()" in updated and "logger.info('refunding')" in updated and updated.count("logger.info('charging')") == 1
    assert report.edits[0].change_indices == [0, 1]


def test_an_add_of_a_new_file_gets_a_trailing_newline(root: Path) -> None:
    add = CodeChange(file_path="services/payment/retry.py", operation="add", explanation="x", new_code="RETRIES = 3")
    report = validate_changes(root, [add], read_paths={APP})  # an add needs no read of its own path
    assert report.ok and report.edits[0].original is None and report.edits[0].updated == "RETRIES = 3\n"


def test_a_delete_of_an_existing_file(root: Path) -> None:
    delete = CodeChange(file_path="services/payment/util.py", operation="delete", explanation="dead code")
    report = validate_changes(root, [delete], read_paths={"services/payment/util.py"})
    assert report.ok and (report.edits[0].original, report.edits[0].updated) == ("X = 1\n", None)


def test_changes_to_different_files_are_validated_independently(root: Path) -> None:
    bad = modify(old="not in the file", path="services/payment/util.py")
    report = validate_changes(root, [modify(), bad], read_paths={APP, "services/payment/util.py"})
    assert not report.ok
    assert report.valid_indices == [0] and report.failed_indices == [1]  # the good file still has its edit
    assert [e.file_path for e in report.edits] == [APP]


def test_without_a_read_set_that_check_is_skipped(root: Path) -> None:
    assert validate_changes(root, [modify()], read_paths=None).ok


# -------------------------------------------------------------------------- matching


def test_old_code_that_occurs_twice_is_ambiguous_and_nothing_is_applied(root: Path) -> None:
    report = validate_changes(root, [modify(old="    logger.info('charging')", new="    pass")], read_paths={APP})
    assert issue_codes(report) == [IssueCode.OLD_CODE_AMBIGUOUS] and "2 times" in report.issues[0].message
    assert report.edits == []


def test_overlapping_occurrences_count_as_ambiguous() -> None:
    assert find_all("aaa", "aa") == [0, 1]  # 'aa' is in 'aaa' twice
    assert find_all("abc", "x") == [] and find_all("abcabc", "abc") == [0, 3]


def test_old_code_that_is_not_in_the_file_is_missing(root: Path) -> None:
    report = validate_changes(root, [modify(old="response = totally.different()")], read_paths={APP})
    assert issue_codes(report) == [IssueCode.OLD_CODE_MISSING] and report.issues[0].found_text is None


def test_a_whitespace_only_difference_is_reported_separately_with_the_files_own_text(root: Path) -> None:
    sloppy = "response = gateway.call(request)   # no timeout set"  # different indentation and spacing
    report = validate_changes(root, [modify(old=sloppy)], read_paths={APP})
    (issue,) = report.issues
    assert issue.code == IssueCode.WHITESPACE_MISMATCH and "copy the file's text exactly" in issue.message
    assert issue.found_text == "response = gateway.call(request)  # no timeout set"  # what to copy; nothing was repaired
    assert report.edits == []


def test_a_loose_match_in_several_places_is_ambiguous_and_a_different_text_is_simply_missing(root: Path) -> None:
    (root / "twice.py").write_text("a = 1  # note\nb = 2\na =   1 # note\n", encoding="utf-8")
    loose = CodeChange(file_path="twice.py", operation="modify", explanation="x", old_code="a = 1 # note", new_code="a = 9")
    report = validate_changes(root, [loose], read_paths={"twice.py"})
    assert issue_codes(report) == [IssueCode.AMBIGUOUS_WHITESPACE] and "2 places ignoring whitespace" in report.issues[0].message
    different_tokens = CodeChange(file_path=APP, operation="modify", explanation="x", old_code="logger.info( 'charging' )", new_code="pass")
    assert issue_codes(validate_changes(root, [different_tokens], read_paths={APP})) == [IssueCode.OLD_CODE_MISSING]  # '( ' is not whitespace-only


def test_whitespace_matching_helper() -> None:
    text = "a  b\n\tc   d"
    assert whitespace_matches(text, "a b c d") == [(0, len(text))]
    assert whitespace_matches("x y x y", "x y") == [(0, 3), (4, 7)] and whitespace_matches(text, "   ") == []


def test_crlf_files_are_matched_with_lf_old_code_and_keep_their_line_endings(tmp_path: Path) -> None:
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    change = CodeChange(file_path="w.txt", operation="modify", explanation="x", old_code="two\nthree", new_code="2\n3")
    report = validate_changes(tmp_path, [change], read_paths={"w.txt"})
    assert report.ok and report.edits[0].updated == "one\r\n2\r\n3\r\n" and report.edits[0].original == "one\r\ntwo\r\nthree\r\n"


def test_a_file_that_mixes_line_endings_is_refused(tmp_path: Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"one\r\ntwo\nthree\r\n")
    change = CodeChange(file_path="m.txt", operation="modify", explanation="x", old_code="two", new_code="2")
    assert issue_codes(validate_changes(tmp_path, [change], read_paths={"m.txt"})) == [IssueCode.MIXED_LINE_ENDINGS]


# ------------------------------------------------------------------------ the files


def test_modify_or_delete_of_a_missing_file_and_add_of_an_existing_one(root: Path) -> None:
    ghost = "services/payment/ghost.py"
    report = validate_changes(
        root,
        [
            CodeChange(file_path=ghost, operation="modify", explanation="x", old_code="a", new_code="b"),
            CodeChange(file_path=ghost, operation="delete", explanation="x"),  # same file as the first: a conflict, see below
        ],
        read_paths={ghost},
    )
    assert set(issue_codes(report)) == {IssueCode.CONFLICT}
    modify_missing = CodeChange(file_path=ghost, operation="modify", explanation="x", old_code="a", new_code="b")
    assert issue_codes(validate_changes(root, [modify_missing], read_paths={ghost})) == [IssueCode.FILE_MISSING]
    delete_missing = CodeChange(file_path=ghost, operation="delete", explanation="x")
    assert issue_codes(validate_changes(root, [delete_missing], read_paths={ghost})) == [IssueCode.FILE_MISSING]
    add_existing = CodeChange(file_path=APP, operation="add", explanation="x", new_code="x = 1")
    assert issue_codes(validate_changes(root, [add_existing])) == [IssueCode.FILE_EXISTS]


def test_files_that_cannot_be_edited_are_refused_not_crashed_on(root: Path) -> None:
    binary = CodeChange(file_path="blob.bin", operation="modify", explanation="x", old_code="a", new_code="b")
    assert issue_codes(validate_changes(root, [binary], read_paths={"blob.bin"})) == [IssueCode.FILE_UNREADABLE]
    (root / "big.txt").write_text("x" * 1_100_000, encoding="utf-8")
    big = CodeChange(file_path="big.txt", operation="modify", explanation="x", old_code="x", new_code="y")
    assert issue_codes(validate_changes(root, [big], read_paths={"big.txt"})) == [IssueCode.FILE_UNREADABLE]
    folder = CodeChange(file_path="services", operation="delete", explanation="x")
    assert issue_codes(validate_changes(root, [folder], read_paths={"services"})) == [IssueCode.FILE_UNREADABLE]


@pytest.mark.parametrize(
    "path",
    [".env", "config/id_rsa", "node_modules/x.js", "dist/app.min.js", ".git/config", "../outside.py", "/etc/passwd", "a\\b.py", "a//b.py", "", "x" * 600],
)
def test_secret_vendored_and_escaping_paths_are_refused_before_any_file_is_read(root: Path, path: str) -> None:
    change = CodeChange(file_path=path, operation="modify", explanation="x", old_code="a", new_code="b")
    report = validate_changes(root, [change], read_paths={path})
    assert issue_codes(report) == [IssueCode.PATH_INVALID] and report.edits == []


def test_a_file_the_investigation_never_read_is_refused(root: Path) -> None:
    report = validate_changes(root, [modify(path="services/payment/util.py", old="X = 1", new="X = 2")], read_paths={APP})
    assert issue_codes(report) == [IssueCode.NOT_READ] and "was not read" in report.issues[0].message


def test_a_folder_link_pointing_out_of_the_checkout_is_not_followed(root: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt" or subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False).returncode != 0:
            pytest.skip("this system does not allow folder links")
    change = CodeChange(file_path="linked/secret.py", operation="modify", explanation="x", old_code="SECRET = 1", new_code="SECRET = 2")
    assert issue_codes(validate_changes(root, [change], read_paths={"linked/secret.py"})) == [IssueCode.FILE_UNREADABLE]
    add = CodeChange(file_path="linked/new.py", operation="add", explanation="x", new_code="x = 1")
    assert issue_codes(validate_changes(root, [add])) == [IssueCode.PATH_INVALID]
    assert not (outside / "new.py").exists()


# ------------------------------------------------------------- overlaps and conflicts


def test_two_edits_touching_the_same_text_overlap(root: Path) -> None:
    a = modify(old="gateway.call(request)", new="gateway.call(request, 1)")
    b = modify(old="call(request)  # no timeout set", new="call(request, 2)")
    report = validate_changes(root, [a, b], read_paths={APP})
    assert issue_codes(report) == [IssueCode.OVERLAP] and "overlaps change" in report.issues[0].message
    assert report.edits == []  # a file with any problem is not partly applied


@pytest.mark.parametrize("operations", [("delete", "modify"), ("modify", "delete"), ("add", "add"), ("add", "modify")])
def test_contradictory_operations_on_one_file_are_conflicts(root: Path, operations: tuple[str, str]) -> None:
    changes = [
        CodeChange(file_path="services/payment/new.py", operation=op, explanation="x", old_code="a" if op == "modify" else None, new_code="b" if op != "delete" else None)  # type: ignore[arg-type]
        for op in operations
    ]
    report = validate_changes(root, changes, read_paths={"services/payment/new.py"})
    assert issue_codes(report) == [IssueCode.CONFLICT, IssueCode.CONFLICT] and report.edits == []


def test_a_change_that_leaves_the_file_identical_is_refused(root: Path) -> None:
    assert issue_codes(validate_changes(root, [modify(old=OLD, new=OLD)], read_paths={APP})) == [IssueCode.NO_CHANGE]


def test_new_code_with_a_nul_byte_and_oversized_new_files_are_refused(root: Path) -> None:
    assert issue_codes(validate_changes(root, [modify(new="a\x00b")], read_paths={APP})) == [IssueCode.BINARY_CONTENT]
    big = CodeChange(file_path="services/payment/big.py", operation="add", explanation="x", new_code="x" * 1_100_000)
    assert issue_codes(validate_changes(root, [big])) == [IssueCode.TOO_LARGE]
    empty = CodeChange(file_path="services/payment/empty.py", operation="add", explanation="x", new_code="  \n ")
    assert issue_codes(validate_changes(root, [empty])) == [IssueCode.NO_CHANGE]
    nul = CodeChange(file_path="services/payment/nul.py", operation="add", explanation="x", new_code="a\x00")
    assert issue_codes(validate_changes(root, [nul])) == [IssueCode.BINARY_CONTENT]


def test_a_growth_past_the_size_limit_after_a_modify_is_refused(root: Path) -> None:
    (root / "near.txt").write_text("marker\n" + "x" * 990_000, encoding="utf-8")
    change = CodeChange(file_path="near.txt", operation="modify", explanation="x", old_code="marker", new_code="y" * 50_000)
    assert issue_codes(validate_changes(root, [change], read_paths={"near.txt"})) == [IssueCode.TOO_LARGE]


# ------------------------------------------------------------------------ the report


def test_the_report_names_the_failing_change_by_its_position_so_a_retry_can_fix_exactly_it(root: Path) -> None:
    changes = [modify(), modify(old="nowhere in the file"), modify(old="also nowhere")]
    report = validate_changes(root, changes, read_paths={APP})
    assert [i.index for i in report.issues] == [1, 2] and report.failed_indices == [1, 2]
    assert all(isinstance(i, ChangeIssue) and i.file_path == APP and i.message for i in report.issues)
    assert report.edits == []  # the file has a failing change, so none of its changes is offered as applied


def test_the_number_of_issues_is_capped(root: Path) -> None:
    changes = [CodeChange(file_path=f"services/payment/f{i}.py", operation="modify", explanation="x", old_code="a", new_code="b") for i in range(60)]
    report = validate_changes(root, changes, read_paths={c.file_path for c in changes})
    assert len(report.issues) == 20


def test_an_empty_list_validates(root: Path) -> None:
    report = validate_changes(root, [])
    assert report.ok and report.edits == [] and report.valid_indices == []
