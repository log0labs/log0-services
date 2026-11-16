"""Check proposed code changes against the repository, before anything is shown or turned into a diff.

The change proposer (4.2) only guarantees a change is well-formed and names a file the model read.
This validator checks the change against **the files themselves**, at the pinned commit:

* the file exists (modify, delete) or does not (add),
* ``old_code`` occurs **exactly once** in the file, counting overlapping occurrences,
* if it does not match exactly but does match **ignoring whitespace**, that is reported separately
  (``whitespace_mismatch``) with the text the file really has, so a retry can copy it exactly,
* the changed files are a subset of the files the investigation read (when the caller says which),
* several edits to one file do not overlap and do not contradict each other (delete plus modify,
  two adds of one path).

It never changes the repository and never guesses: a change that does not match is reported, not
repaired. Every change that passes is turned into the new content of its file (``FileEdit``), ready
for the diff builder of step 4.4; a file is included only when **all** of its changes are valid, so a
half-applied file can never reach a diff.

The files are read through ``load_text_file``, the same guarded opener as ``read_file`` (no paths
outside the checkout, no ``.git``, no secret-bearing names, text files up to 1 MB), so the validator
cannot be tricked into reading what the tools refuse to show.
"""

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from pydantic import BaseModel, Field

from app.schemas.domain import CodeChange
from app.tools.code import (
    MAX_FILE_BYTES,
    CodeToolError,
    check_repo_path,
    is_excluded,
    load_text_file,
)

logger = logging.getLogger(__name__)

MAX_FOUND_TEXT = 400  # the text of the file quoted back to the model in a whitespace mismatch
MAX_ISSUES = 20


class IssueCode(StrEnum):
    PATH_INVALID = "path_invalid"  # not a usable repository path, or secret-bearing / vendored
    NOT_READ = "not_read"  # the investigation never read this file
    FILE_MISSING = "file_missing"  # modify or delete of a file that does not exist
    FILE_EXISTS = "file_exists"  # add of a file that already exists
    FILE_UNREADABLE = "file_unreadable"  # binary, too large, or refused by the file guards
    OLD_CODE_MISSING = "old_code_missing"  # no match, not even ignoring whitespace
    WHITESPACE_MISMATCH = "whitespace_mismatch"  # exactly one match, but only ignoring whitespace
    OLD_CODE_AMBIGUOUS = "old_code_ambiguous"  # more than one exact match
    AMBIGUOUS_WHITESPACE = "ambiguous_whitespace"  # no exact match; more than one ignoring whitespace
    OVERLAP = "overlap"  # two edits to one file touch the same text
    CONFLICT = "conflict"  # edits that contradict each other (delete plus modify, add twice)
    MIXED_LINE_ENDINGS = "mixed_line_endings"  # the file mixes LF and CRLF: no safe way to edit it
    NO_CHANGE = "no_change"  # the file would be identical afterwards
    TOO_LARGE = "too_large"  # the new file would exceed the size limit
    BINARY_CONTENT = "binary_content"  # new code contains a NUL byte


class ChangeIssue(BaseModel):
    index: int  # position in the list that was validated: lets a retry fix exactly this change
    file_path: str
    code: IssueCode
    message: str  # written to be read by the model that proposed the change
    found_text: str | None = Field(default=None, description="For a whitespace mismatch: what the file actually contains")


class FileEdit(BaseModel):
    """One file as it is and as it would be. ``original`` is ``None`` for an add, ``updated`` for a delete."""

    file_path: str
    original: str | None
    updated: str | None
    change_indices: list[int]  # which of the validated changes produced this edit


class ValidationReport(BaseModel):
    issues: list[ChangeIssue] = Field(default_factory=list)
    edits: list[FileEdit] = Field(default_factory=list)  # only files whose every change is valid

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def valid_indices(self) -> list[int]:
        return sorted(i for edit in self.edits for i in edit.change_indices)

    @property
    def failed_indices(self) -> list[int]:
        return sorted({issue.index for issue in self.issues})


# ------------------------------------------------------------------------- matching


def find_all(content: str, needle: str) -> list[int]:
    """Start offsets of every occurrence, **overlapping ones included**: ``aa`` occurs twice in ``aaa``,
    so replacing "the one place" would be a guess."""
    starts: list[int] = []
    position = content.find(needle)
    while position != -1:
        starts.append(position)
        position = content.find(needle, position + 1)
    return starts


def whitespace_matches(content: str, needle: str) -> list[tuple[int, int]]:
    """Spans of ``content`` that equal ``needle`` once every run of whitespace counts as one."""
    tokens = needle.split()
    if not tokens:
        return []
    pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens))
    return [(m.start(), m.end()) for m in pattern.finditer(content)]


# ------------------------------------------------------------------------ one file


@dataclass
class _Loaded:
    text: str  # content with LF line endings
    crlf: bool


def _load(root: Path, path: str) -> _Loaded | IssueCode:
    try:
        _, data = load_text_file(root, path)
    except CodeToolError as exc:
        message = str(exc)
        return IssueCode.FILE_MISSING if "not found" in message else IssueCode.FILE_UNREADABLE
    text = data.decode("utf-8", errors="replace")
    crlf = "\r\n" in text
    if crlf and re.search(r"(?<!\r)\n", text):
        return IssueCode.MIXED_LINE_ENDINGS
    return _Loaded(text.replace("\r\n", "\n"), crlf)


def _lf(value: str | None) -> str | None:
    return None if value is None else value.replace("\r\n", "\n")


def _exists(root: Path, path: str) -> bool:
    """Whether the path names something in the checkout (any kind of entry)."""
    try:
        return (root.resolve() / path).exists()
    except OSError:
        return True  # unknowable: refuse the add rather than overwrite something


def _validate_file(
    root: Path, path: str, group: list[tuple[int, CodeChange]], issues: list[ChangeIssue]
) -> FileEdit | None:
    """All the changes to one file. Returns its edit, or ``None`` after recording at least one issue."""
    first = len(issues)

    def problem(index: int, code: IssueCode, message: str, found: str | None = None) -> None:
        issues.append(ChangeIssue(index=index, file_path=path, code=code, message=message, found_text=found))

    operations = [change.operation for _, change in group]
    if len(group) > 1 and any(operation != "modify" for operation in operations):
        for index, change in group:
            problem(index, IssueCode.CONFLICT, f"several changes to {path} contradict each other ({', '.join(operations)}): propose one coherent change per file")
        return None

    index, change = group[0]
    if change.operation == "add":
        if not (root.resolve() / path).resolve().is_relative_to(root.resolve()):  # a folder link pointing out of the checkout
            problem(index, IssueCode.PATH_INVALID, f"{path} leaves the repository")
            return None
        if _exists(root, path):
            problem(index, IssueCode.FILE_EXISTS, f"{path} already exists: use modify, not add")
            return None
        new = _lf(change.new_code) or ""
        if not new.strip():
            problem(index, IssueCode.NO_CHANGE, "the new file would be empty")
            return None
        if "\x00" in new:
            problem(index, IssueCode.BINARY_CONTENT, "the new file contains a NUL byte: only text files can be added")
            return None
        if len(new.encode("utf-8")) > MAX_FILE_BYTES:
            problem(index, IssueCode.TOO_LARGE, "the new file is larger than 1 MB")
            return None
        return FileEdit(file_path=path, original=None, updated=new if new.endswith("\n") else new + "\n", change_indices=[index])

    loaded = _load(root, path)
    if isinstance(loaded, IssueCode):
        reasons = {
            IssueCode.FILE_MISSING: f"{path} does not exist at the pinned commit",
            IssueCode.MIXED_LINE_ENDINGS: f"{path} mixes LF and CRLF line endings, so it cannot be edited safely",
            IssueCode.FILE_UNREADABLE: f"{path} cannot be edited (binary, larger than 1 MB, or not available)",
        }
        for index, _change in group:
            problem(index, loaded, reasons[loaded])
        return None

    if change.operation == "delete":
        return FileEdit(file_path=path, original=_original(loaded), updated=None, change_indices=[index])

    # modify: find where each edit goes in the ORIGINAL text
    spans: list[tuple[int, int, str, int]] = []  # (start, end, replacement, change index)
    for index, change in group:
        old, new = _lf(change.old_code) or "", _lf(change.new_code) or ""
        if "\x00" in new:
            problem(index, IssueCode.BINARY_CONTENT, "new_code contains a NUL byte")
            continue
        starts = find_all(loaded.text, old)
        if len(starts) == 1:
            spans.append((starts[0], starts[0] + len(old), new, index))
        elif len(starts) > 1:
            problem(index, IssueCode.OLD_CODE_AMBIGUOUS, f"old_code occurs {len(starts)} times in {path}: include more surrounding lines so it occurs exactly once")
        else:
            near = whitespace_matches(loaded.text, old)
            if len(near) == 1:
                found = loaded.text[near[0][0] : near[0][1]]
                problem(
                    index,
                    IssueCode.WHITESPACE_MISMATCH,
                    f"old_code matches {path} only if whitespace is ignored: copy the file's text exactly, character for character",
                    found[:MAX_FOUND_TEXT],
                )
            elif len(near) > 1:
                problem(index, IssueCode.AMBIGUOUS_WHITESPACE, f"old_code does not match {path} exactly, and matches {len(near)} places ignoring whitespace: copy the text exactly and include more lines")
            else:
                problem(index, IssueCode.OLD_CODE_MISSING, f"old_code does not occur in {path}: copy it exactly from the file")
    if len(issues) > first:
        return None

    spans.sort()
    for (_s1, e1, _r1, i1), (s2, _e2, _r2, i2) in pairwise(spans):
        if s2 < e1:
            problem(i2, IssueCode.OVERLAP, f"this change overlaps change {i1} in {path}: merge them into one")
    if len(issues) > first:
        return None

    updated, cursor = [], 0
    for start, end, replacement, _index in spans:
        updated += [loaded.text[cursor:start], replacement]
        cursor = end
    updated.append(loaded.text[cursor:])
    result = "".join(updated)
    if result == loaded.text:
        problem(group[0][0], IssueCode.NO_CHANGE, f"the change leaves {path} identical")
        return None
    if len(result.encode("utf-8")) > MAX_FILE_BYTES:
        problem(group[0][0], IssueCode.TOO_LARGE, f"{path} would be larger than 1 MB after the change")
        return None
    return FileEdit(
        file_path=path,
        original=_original(loaded),
        updated=result.replace("\n", "\r\n") if loaded.crlf else result,
        change_indices=sorted(index for *_rest, index in spans),
    )


def _original(loaded: _Loaded) -> str:
    return loaded.text.replace("\n", "\r\n") if loaded.crlf else loaded.text


# ------------------------------------------------------------------------ all files


def validate_changes(root: Path, changes: list[CodeChange], *, read_paths: Iterable[str] | None = None) -> ValidationReport:
    """Validate ``changes`` against the checkout at ``root`` (a snapshot at the pinned commit).

    ``read_paths`` is the set of files the investigation actually read; a change to any other file
    is refused (``not_read``). Pass ``None`` to skip that check. Never raises for bad input: every
    problem becomes a ``ChangeIssue``.
    """
    issues: list[ChangeIssue] = []
    edits: list[FileEdit] = []
    allowed = None if read_paths is None else set(read_paths)

    groups: dict[str, list[tuple[int, CodeChange]]] = {}
    for index, change in enumerate(changes):
        try:
            path = check_repo_path(change.file_path)
        except CodeToolError as exc:
            issues.append(ChangeIssue(index=index, file_path=change.file_path[:200], code=IssueCode.PATH_INVALID, message=str(exc)))
            continue
        if is_excluded(path):
            issues.append(ChangeIssue(index=index, file_path=path, code=IssueCode.PATH_INVALID, message=f"{path} is secret-bearing or vendored: it cannot be changed"))
            continue
        if allowed is not None and change.operation != "add" and path not in allowed:
            issues.append(ChangeIssue(index=index, file_path=path, code=IssueCode.NOT_READ, message=f"{path} was not read during the investigation: read it before changing it"))
            continue
        groups.setdefault(path, []).append((index, change))

    for path, group in groups.items():
        edit = _validate_file(root, path, group, issues)
        if edit is not None:
            edits.append(edit)
        if len(issues) >= MAX_ISSUES:
            logger.info("validation stopped after %d issues", len(issues))
            break
    return ValidationReport(issues=issues[:MAX_ISSUES], edits=edits)
