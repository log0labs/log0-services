"""Which changed files the root cause names, and how that shows on the approval request and in the pull request."""

import pytest

from app.diff.builder import FileDiff
from app.publishing.naming import pull_request_body
from app.publishing.scope import cited_paths, outside_root_cause
from app.schemas.domain import CodeChange, CodeLocation, Evidence, RootCause


def code_evidence(path: str, line: str = "9") -> Evidence:
    return Evidence(id=f"ev_{abs(hash(path)) % 10**8:08d}", kind="code", source_ref=f"{path}:{line}@a91c3e5", summary="x")


def cause(*, locations: list[str] | None = None, evidence: list[Evidence] | None = None) -> RootCause:
    return RootCause(
        statement="pool too small",
        evidence_ids=[e.id for e in evidence or []],
        affected_locations=[CodeLocation(file_path=p) for p in locations or []],
    )


def test_a_file_is_cited_by_an_affected_location_or_by_code_evidence_the_cause_cites() -> None:
    cited_evidence = code_evidence("services/api/src/db.js", "9")
    other_evidence = code_evidence("services/api/src/seed.js")  # read, but not cited by the cause

    paths = cited_paths(cause(locations=["services/api/src/pool.js"], evidence=[cited_evidence]), [cited_evidence, other_evidence])

    assert paths == {"services/api/src/pool.js", "services/api/src/db.js"}


def test_only_code_evidence_counts_and_line_ranges_are_stripped() -> None:
    log = Evidence(id="ev_00000001", kind="log", source_ref="payments/app.py:12", summary="x")
    ranged = Evidence(id="ev_00000002", kind="code", source_ref="src/a.go:10-20@abc", summary="x")

    assert cited_paths(RootCause(statement="s", evidence_ids=["ev_00000001", "ev_00000002"]), [log, ranged]) == {"src/a.go"}


@pytest.mark.parametrize(
    ("changed", "flagged"),
    [
        (["services/api/src/db.js"], set()),
        (["services/api/src/db.js", "services/api/src/seed.js"], {"services/api/src/seed.js"}),
        (["src/db.js"], set()),  # a path written relative to the service still matches
        (["services/api/test/db.test.js", "services/api/src/db_test.go", "tests/test_db.py", "pkg/__tests__/a.ts"], set()),  # tests are never extra
        (["services\\api\\src\\db.js"], set()),
    ],
)
def test_changed_files_the_cause_does_not_name_are_flagged_and_tests_are_not(changed: list[str], flagged: set[str]) -> None:
    assert outside_root_cause(changed, {"services/api/src/db.js"}) == flagged


def test_when_nothing_is_cited_nothing_is_flagged() -> None:
    assert outside_root_cause(["a.js", "b.js"], set()) == set()  # no way to tell, so no accusation


def test_the_pull_request_marks_extra_files_and_says_what_to_do() -> None:
    files = [FileDiff(file_path="src/db.js", operation="modify", additions=1, deletions=2), FileDiff(file_path="src/seed.js", operation="modify", additions=1, deletions=0)]
    changes = [CodeChange(file_path="src/db.js", operation="modify", explanation="bigger pool")]

    body = pull_request_body(root_cause=cause(), confidence_band=None, changes=changes, files=files, outside_root_cause={"src/seed.js"})

    assert "- `src/seed.js` (modify, +1 -0) **(not named in the root cause)**" in body
    assert "- `src/db.js` (modify, +1 -2)" in body and "(modify, +1 -2) **" not in body
    assert "drop them if the fix does not need them" in body
    clean = pull_request_body(root_cause=cause(), confidence_band=None, changes=changes, files=files[:1])
    assert "not named in the root cause" not in clean and "drop them" not in clean
