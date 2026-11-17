"""Failures that cross services: what the graph can see of the other services (code folders, co-occurring errors) and how sure it may sound."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from app.graph.code_access import CodeCheckout, shared_prefix
from app.graph.nodes.root_cause import cap_by_gaps
from app.schemas.domain import RepoContext
from app.tools.code import CodeTools
from app.tools.git import GitTools
from app.tools.logs import get_cooccurring_errors
from tests.conftest import requires_postgres
from tests.remediation_support import (
    forget,
    seed,
    session_factory,  # noqa: F401  (fixture)
)
from tests.test_log_tools import T0, TENANT, FakeBackend

# ------------------------------------------------------------------ the folder that covers the sibling services


@pytest.mark.parametrize(
    ("prefix", "siblings", "expected"),
    [
        ("services/storefront-api", ("services/inventory", "services/payments"), "services"),
        ("services/storefront-api", ("services/storefront-api-tests",), "services"),  # a name that merely starts the same is a different folder
        ("services/a", ("apps/b",), ""),  # nothing in common: the whole repository
        ("services/a/api", ("services/a/db",), "services/a"),
        ("services/storefront-api", (), "services/storefront-api"),  # no siblings, nothing changes
        ("", ("services/inventory",), ""),  # a service that already owns the whole repository stays that way
    ],
)
def test_the_shared_folder_holds_the_service_and_its_siblings(prefix: str, siblings: tuple[str, ...], expected: str) -> None:
    assert shared_prefix(prefix, siblings) == expected


def _checkout(prefix: str, siblings: tuple[str, ...]) -> CodeCheckout:
    root = Path(".")
    return CodeCheckout(
        repo=RepoContext(repo="acme/shop", branch="main", sha="a" * 40, path_prefix=prefix),
        code=CodeTools(root, prefix=prefix),
        git=GitTools(root, prefix=prefix),
        siblings=siblings,
    )


def test_a_widened_checkout_searches_the_sibling_services_too_and_the_original_is_untouched() -> None:
    original = _checkout("services/storefront-api", ("services/inventory",))

    wide = original.widened()

    assert wide.code.prefix == "services" and wide.git.prefix == "services"
    assert original.code.prefix == "services/storefront-api"  # frozen: widening makes a new checkout
    assert wide.repo == original.repo  # the commit and the service folder that were pinned do not change


def test_a_checkout_without_siblings_is_returned_as_it_is() -> None:
    checkout = _checkout("services/storefront-api", ())

    assert checkout.widened() is checkout


def test_a_checkout_built_from_something_else_than_the_real_tools_is_left_alone() -> None:
    @dataclass(frozen=True)
    class Plain:
        pass

    checkout = CodeCheckout(repo=RepoContext(repo="a/b", branch="main", sha="a" * 40, path_prefix="services/x"), code=Plain(), git=Plain(), siblings=("services/y",))  # type: ignore[arg-type]

    assert checkout.widened().code.__class__ is Plain


# ------------------------------------------------------------------ the co-occurring errors


@pytest.mark.asyncio
async def test_each_service_gets_its_own_share_so_a_noisy_one_cannot_hide_a_quiet_one() -> None:
    backend = FakeBackend([])

    await get_cooccurring_errors(backend, tenant_id=TENANT, start=T0, end=T0 + timedelta(minutes=10), limit=10)

    sql, _, parameters = backend.calls[0]
    assert "LIMIT {per_service:UInt32} BY service_name" in sql and parameters["per_service"] == 3
    assert "ORDER BY n DESC" in sql and "onset" not in sql  # without an onset the old order stays


@pytest.mark.asyncio
async def test_what_began_with_the_incident_comes_before_older_noise() -> None:
    backend = FakeBackend([])
    onset = T0 + timedelta(minutes=5)

    await get_cooccurring_errors(backend, tenant_id=TENANT, start=T0, end=T0 + timedelta(minutes=10), onset=onset, per_service=2)

    sql, _, parameters = backend.calls[0]
    assert "ORDER BY (first_ts >= {onset:DateTime64(3)}) DESC, n DESC" in sql
    assert parameters["onset"] == onset and parameters["per_service"] == 2
    assert str(onset) not in sql  # a bound parameter, never text in the query


@pytest.mark.asyncio
async def test_a_per_service_share_is_never_larger_than_the_limit_or_smaller_than_one() -> None:
    backend = FakeBackend([])

    await get_cooccurring_errors(backend, tenant_id=TENANT, start=T0, end=T0 + timedelta(minutes=10), limit=2, per_service=50)
    await get_cooccurring_errors(backend, tenant_id=TENANT, start=T0, end=T0 + timedelta(minutes=10), limit=5, per_service=0)

    assert [c[2]["per_service"] for c in backend.calls] == [2, 1]


# ------------------------------------------------------------------ how sure the graph may sound


def test_an_investigation_that_stopped_at_a_limit_cannot_claim_high_confidence() -> None:
    band, why = cap_by_gaps("high", ["The code investigation stopped early because the step limit (10) was reached"])

    assert band == "medium" and why is not None and "limit" in why


@pytest.mark.parametrize("gaps", [[], ["The model gave no recommendation"], ["Past incidents were not searched"]])
def test_other_gaps_do_not_lower_confidence(gaps: list[str]) -> None:
    assert cap_by_gaps("high", gaps) == ("high", None)


@pytest.mark.parametrize("band", ["medium", "low", None])
def test_a_band_that_is_not_high_is_never_changed(band: str | None) -> None:
    assert cap_by_gaps(band, ["stopped early because the limit was reached"]) == (band, None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the sibling folders come from the mapping (real Postgres)


@requires_postgres
@pytest.mark.asyncio
async def test_the_sibling_folders_are_the_other_services_mapped_to_the_same_repository(session_factory) -> None:  # noqa: F811
    from app.store import repositories as repository_store

    seeded = await seed(session_factory)
    other = await seed(session_factory)  # another workspace with its own repository
    try:
        async with session_factory() as session:
            for service, folder in (("storefront-api", "services/storefront-api"), ("inventory", "services/inventory"), ("docs-site", "")):
                await repository_store.upsert_service(session, tenant_id=seeded.tenant, service_name=service, repository_id=seeded.repository_id, path_prefix=folder)
            await repository_store.upsert_service(session, tenant_id=other.tenant, service_name="their-service", repository_id=other.repository_id, path_prefix="services/theirs")

            mine = await repository_store.mapped_prefixes(session, tenant_id=seeded.tenant, repository_id=seeded.repository_id)
            theirs_asked_with_my_repository = await repository_store.mapped_prefixes(session, tenant_id=other.tenant, repository_id=seeded.repository_id)

        assert mine == ["services/inventory", "services/storefront-api"]  # the whole-repository mapping adds no folder
        assert theirs_asked_with_my_repository == []  # another workspace never sees them
    finally:
        await forget(session_factory, seeded.tenant)
        await forget(session_factory, other.tenant)


# ------------------------------------------------------------------ other services' log lines, found in their code

import shutil
import subprocess

from app.graph.nodes.gather_code import (
    literal_anchor,
    other_service_log_sites,
)
from app.integrations.clickhouse import ClickHouseError
from app.tools.incidents import IncidentContext
from tests.graph_fakes import (
    FINGERPRINT,
    INCIDENT_ID,
    FakeLogs,
    make_context,
)
from tests.test_gather_nodes import FIRST, LAST


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("hold expired for order <uuid>: stock returned", "hold expired for order"),
        ("POST /checkout failed: not enough stock for SKU-<number>", "POST /checkout failed: not enough stock for SKU"),
        ("<number>", None),  # nothing fixed to search for
        ("retry <number>/<number>", None),  # too short to point anywhere
        ("plain message without any variable part", "plain message without any variable part"),
    ],
)
def test_the_fixed_text_of_a_log_message_is_what_is_searched_for(template: str, expected: str | None) -> None:
    assert literal_anchor(template) == expected


def _incident() -> IncidentContext:
    return IncidentContext(
        incident_id=INCIDENT_ID,
        fingerprint=FINGERPRINT,
        service_name="storefront-api",
        environment="production",
        severity="HIGH",
        status="NEW",
        occurrence_count=16,
        first_seen_at=FIRST,
        last_seen_at=LAST,
        resolved_at=None,
        top_messages=["POST /checkout failed"],
        ai_summary=None,
    )


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "services/inventory/src").mkdir(parents=True)
    (tmp_path / "services/storefront-api/src").mkdir(parents=True)
    (tmp_path / "services/inventory/src/main.rs").write_text('fn sweep() {\n    log("WARN", &format!("hold expired for order {order_id}: stock returned"));\n}\n')
    (tmp_path / "services/storefront-api/src/app.js").write_text("// no log of that text here\n")
    for command in (["init", "-q"], ["add", "."], ["-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "x"]):
        subprocess.run(["git", *command], cwd=tmp_path, check=True)
    return tmp_path


def _neighbour(service: str, template: str, first_seen, count: int = 16) -> dict:
    return {"service_name": service, "fingerprint": "c" * 64, "message_template": template, "n": count, "first_ts": first_seen, "last_ts": LAST}


@pytest.mark.skipif(shutil.which("rg") is None or shutil.which("git") is None, reason="ripgrep and git are needed")
@pytest.mark.asyncio
async def test_a_failure_in_another_service_is_traced_to_the_code_that_logs_it(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    checkout = CodeCheckout(
        repo=RepoContext(repo="acme/shop", branch="main", sha="a" * 40, path_prefix="services/storefront-api"),
        code=CodeTools(root, prefix="services/storefront-api"),
        git=GitTools(root, prefix="services/storefront-api"),
        siblings=("services/inventory",),
    )
    rows = [
        _neighbour("inventory", "hold expired for order <uuid>: stock returned", FIRST),
        _neighbour("gateway", "payment gateway unavailable, retry <number>/<number>", FIRST - timedelta(minutes=25), count=9000),  # old noise
    ]

    lines, calls = await other_service_log_sites(_incident(), make_context(logs=FakeLogs(cooccurring_rows=rows)), checkout)

    assert lines == ['inventory logged "hold expired for order <uuid>: stock returned" (16x) from services/inventory/src/main.rs:2']
    assert calls == 2  # one query for the neighbours and one code search; the old noise and the short message cost nothing


@pytest.mark.asyncio
async def test_a_log_store_that_cannot_be_read_just_means_no_extra_lines(tmp_path: Path) -> None:
    checkout = CodeCheckout(
        repo=RepoContext(repo="acme/shop", branch="main", sha="a" * 40, path_prefix="services/storefront-api"),
        code=CodeTools(tmp_path),
        git=GitTools(tmp_path),
        siblings=("services/inventory",),
    )

    lines, _ = await other_service_log_sites(_incident(), make_context(logs=FakeLogs(errors={"cooccurring": ClickHouseError("down")})), checkout)

    assert lines == []


# ------------------------------------------------------------------ the suspect commit, opened up into the code it changed

from app.graph.nodes.gather_code import (
    changed_ranges,
    commit_code_evidence,
)
from app.graph.nodes.root_cause import cap_without_code
from app.schemas.domain import CodeLocation, Evidence, RootCause

PATCH = """\
commit abc
diff --git a/services/inventory/src/stock.rs b/services/inventory/src/stock.rs
index 1..2 100644
--- a/services/inventory/src/stock.rs
+++ b/services/inventory/src/stock.rs
@@ -10,6 +10,9 @@ impl Stock {
+a
@@ -60,2 +66,12 @@ impl Stock {
+b
diff --git a/services/storefront-api/src/app.js b/services/storefront-api/src/app.js
--- a/services/storefront-api/src/app.js
+++ b/services/storefront-api/src/app.js
@@ -95,3 +95,8 @@ app.post
+c
diff --git a/services/storefront-api/test/checkout.test.js b/services/storefront-api/test/checkout.test.js
--- /dev/null
+++ b/services/storefront-api/test/checkout.test.js
@@ -0,0 +1,83 @@
+t
diff --git a/old.txt b/old.txt
--- a/old.txt
+++ /dev/null
@@ -1,4 +0,0 @@
-gone
"""


def test_each_changed_place_is_its_own_range_and_tests_and_deletions_are_left_out() -> None:
    assert changed_ranges(PATCH) == [
        ("services/inventory/src/stock.rs", 10, 18),
        ("services/inventory/src/stock.rs", 66, 77),
        ("services/storefront-api/src/app.js", 95, 102),
    ]


def test_close_hunks_merge_and_long_ranges_are_cut_into_pieces() -> None:
    near = "diff --git a/a.py b/a.py\n@@ -1,1 +1,3 @@\n+x\n@@ -9,1 +9,3 @@\n+y\n"
    assert changed_ranges(near) == [("a.py", 1, 11)]
    big = "diff --git a/b.py b/b.py\n@@ -0,0 +1,150 @@\n+x\n"
    assert changed_ranges(big) == [("b.py", 1, 60), ("b.py", 61, 120), ("b.py", 121, 150)]


def test_only_the_first_few_files_are_kept() -> None:
    many = "".join(f"diff --git a/f{i}.py b/f{i}.py\n@@ -1,1 +1,2 @@\n+x\n" for i in range(9))

    assert [name for name, _, _ in changed_ranges(many, limit=3)] == ["f0.py", "f1.py", "f2.py"]


def _commit_all(root: Path, message: str) -> str:
    for command in (["add", "."], ["-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", message]):
        subprocess.run(["git", *command], cwd=root, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(shutil.which("rg") is None or shutil.which("git") is None, reason="ripgrep and git are needed")
@pytest.mark.asyncio
async def test_a_suspect_commit_becomes_code_evidence_with_the_lines_it_changed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "services/inventory/src/main.rs").write_text('fn sweep() {\n    log("WARN", "renewed");\n    log("WARN", &format!("hold expired for order {order_id}: stock returned"));\n}\n')
    sha = _commit_all(root, "feat: holds that expire\n\nbody")
    checkout = CodeCheckout(
        repo=RepoContext(repo="acme/shop", branch="main", sha=sha, path_prefix="services/storefront-api"),
        code=CodeTools(root, prefix="services/storefront-api"),
        git=GitTools(root, prefix="services/storefront-api"),
        siblings=("services/inventory",),
    )

    found, calls = await commit_code_evidence(sha, checkout)

    assert [e.kind for e in found] == ["code"] and found[0].source_ref.startswith("services/inventory/src/main.rs:") and sha[:8] in found[0].source_ref
    assert "changed by commit" in found[0].summary and "feat: holds that expire" in found[0].summary
    assert "renewed" in found[0].content  # read from the repository, with line numbers
    assert calls == 2  # the commit and the one file it changed (outside the service folder: a sibling service)


@pytest.mark.asyncio
async def test_a_commit_that_cannot_be_opened_adds_nothing(tmp_path: Path) -> None:
    checkout = CodeCheckout(
        repo=RepoContext(repo="a/b", branch="main", sha="a" * 40, path_prefix=""),
        code=CodeTools(tmp_path),
        git=GitTools(tmp_path),
    )

    assert await commit_code_evidence("f" * 40, checkout) == ([], 1)


def _cause(*ids: str) -> RootCause:
    return RootCause(statement="s", evidence_ids=list(ids), hypothesis_id="h", contributing_factors=[], unresolved_questions=[], model_confidence=0.9)


def _evidence(id_: str, kind: str) -> Evidence:
    return Evidence(id=id_, kind=kind, source_ref=id_, summary="x", content="")  # type: ignore[arg-type]


def test_a_cause_without_a_code_location_is_not_high_confidence_once_the_code_was_read() -> None:
    evidence = [_evidence("ev_1", "log"), _evidence("ev_2", "commit"), _evidence("ev_3", "code")]

    band, why = cap_without_code("high", _cause("ev_1", "ev_2"), evidence, code_read=True)
    assert band == "medium" and why is not None and "no code location" in why

    assert cap_without_code("high", _cause("ev_1", "ev_3"), evidence, code_read=True)[0] == "medium"  # cites code but names no location
    located = _cause("ev_1", "ev_3").model_copy(update={"affected_locations": [CodeLocation(file_path="a.rs", line=3)]})
    assert cap_without_code("high", located, evidence, code_read=True) == ("high", None)
    assert cap_without_code("high", _cause("ev_1", "ev_2"), evidence, code_read=False) == ("high", None)  # no repository to read
    assert cap_without_code("low", _cause("ev_1"), evidence, code_read=True) == ("low", None)
