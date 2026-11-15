"""Which changed files the root cause actually points at. Pure functions.

A fix can carry a change nobody asked for: the model read another file while investigating and improved it too. The change is
grounded, so no rule refuses it, but the engineer approving a pull request should see at a glance which files are the cause
and which are extra. A file counts as part of the root cause when the cause names it as an affected location or cites code
evidence from it; test files are never flagged (a good fix comes with one).
"""

import re
from collections.abc import Iterable

from app.schemas.domain import Evidence, RootCause

_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|[._-](test|spec)\.[a-z0-9]+$|_test\.[a-z0-9]+$|(^|/)test_[^/]+$", re.IGNORECASE)
_LINE_SUFFIX = re.compile(r":\d+(-\d+)?$")


def _normal(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").strip("/")


def cited_paths(cause: RootCause, evidence: Iterable[Evidence]) -> set[str]:
    """Files the root cause names: its affected locations, and the code evidence it cites (``path:line@sha``)."""
    paths = {_normal(location.file_path) for location in cause.affected_locations}
    cited = set(cause.evidence_ids)
    for item in evidence:
        if item.id in cited and item.kind == "code":
            paths.add(_normal(_LINE_SUFFIX.sub("", item.source_ref.split("@", 1)[0])))
    return {p for p in paths if p}


def _same(a: str, b: str) -> bool:
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def outside_root_cause(changed: Iterable[str], cited: set[str]) -> set[str]:
    """Changed files that are neither cited by the root cause nor tests. Empty when nothing is cited (no way to tell)."""
    if not cited:
        return set()
    return {
        path
        for path in changed
        if not _TEST_PATH.search(_normal(path)) and not any(_same(_normal(path), c) for c in cited)
    }
