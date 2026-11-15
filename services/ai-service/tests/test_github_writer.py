"""The GitHub writer (branch, commit, draft pull request) against a mocked GitHub: no network."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.integrations.github import GitHubAccessError, GitHubUnavailableError
from app.integrations.github_app import InstallationToken
from app.integrations.github_writer import (
    BranchRefusedError,
    FileWrite,
    GitHubWriter,
    WriteRejectedError,
    WriteTarget,
    commit_marker,
)

pytestmark = pytest.mark.asyncio

REPO = "https://api.github.com/repos/acme/shop"
TARGET = WriteTarget(owner="acme", name="shop", installation_id=42, github_repository_id=7, default_branch="main")
BASE = "a" * 40
NEW = "b" * 40
BRANCH = "log0/fix-abc-def"
INVESTIGATION = "11111111-1111-4111-8111-111111111111"


class FakeAuth:
    def __init__(self) -> None:
        self.asked: list[tuple[int, list[int] | None]] = []

    async def installation_token(self, installation_id: int, repository_ids=None, permissions=None) -> InstallationToken:
        self.asked.append((installation_id, list(repository_ids or [])))
        return InstallationToken(SecretStr("ghs_write_token"), datetime.now(UTC) + timedelta(hours=1))


@pytest.fixture
def writer() -> tuple[GitHubWriter, FakeAuth]:
    auth = FakeAuth()
    return GitHubWriter(Settings(_env_file=None), auth), auth  # type: ignore[call-arg, arg-type]


def _ref(sha: str) -> httpx.Response:
    return httpx.Response(200, json={"object": {"sha": sha}})


# ------------------------------------------------------------------------ branch


@respx.mock
async def test_a_new_branch_is_created_at_the_base_commit_with_a_repository_scoped_token(writer) -> None:
    w, auth = writer
    respx.get(f"{REPO}/git/ref/heads/{BRANCH}").mock(return_value=httpx.Response(404))
    create = respx.post(f"{REPO}/git/refs").mock(return_value=httpx.Response(201, json={}))

    state = await w.ensure_branch(TARGET, BRANCH, BASE, INVESTIGATION)

    assert (state.name, state.head_sha, state.created) == (BRANCH, BASE, True)
    assert json.loads(create.calls.last.request.content) == {"ref": f"refs/heads/{BRANCH}", "sha": BASE}
    assert create.calls.last.request.headers["authorization"] == "Bearer ghs_write_token"
    assert auth.asked == [(42, [7])]


@respx.mock
async def test_a_branch_at_the_base_commit_is_reused(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/git/ref/heads/{BRANCH}").mock(return_value=_ref(BASE))
    create = respx.post(f"{REPO}/git/refs")

    state = await w.ensure_branch(TARGET, BRANCH, BASE, INVESTIGATION)

    assert state.created is False and state.head_sha == BASE
    assert not create.called


@respx.mock
async def test_a_branch_that_moved_is_ours_only_if_its_commit_carries_the_marker(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/git/ref/heads/{BRANCH}").mock(return_value=_ref(NEW))
    respx.get(f"{REPO}/git/commits/{NEW}").mock(return_value=httpx.Response(200, json={"message": f"fix\n\n{commit_marker(INVESTIGATION)}"}))

    assert (await w.ensure_branch(TARGET, BRANCH, BASE, INVESTIGATION)).head_sha == NEW


@respx.mock
async def test_someone_elses_branch_is_refused(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/git/ref/heads/{BRANCH}").mock(return_value=_ref(NEW))
    respx.get(f"{REPO}/git/commits/{NEW}").mock(return_value=httpx.Response(200, json={"message": "a human commit"}))

    with pytest.raises(BranchRefusedError):
        await w.ensure_branch(TARGET, BRANCH, BASE, INVESTIGATION)


@respx.mock
@pytest.mark.parametrize("name", ["main", "feature/x", "log0", "log0/../main", "log0/x/", "release"])
async def test_branches_outside_log0_and_the_default_branch_are_never_touched(writer, name: str) -> None:
    w, _ = writer
    route = respx.route()

    for call in (
        w.ensure_branch(TARGET, name, BASE, INVESTIGATION),
        w.commit_files(TARGET, name, BASE, [FileWrite("a.py", "x")], "m"),
        w.open_draft_pull_request(TARGET, name, "t", "b"),
    ):
        with pytest.raises(BranchRefusedError):
            await call
    assert not route.called


@respx.mock
async def test_an_unknown_base_commit_is_a_clear_rejection(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/git/ref/heads/{BRANCH}").mock(return_value=httpx.Response(404))
    respx.post(f"{REPO}/git/refs").mock(return_value=httpx.Response(422))

    with pytest.raises(WriteRejectedError):
        await w.ensure_branch(TARGET, BRANCH, BASE, INVESTIGATION)


# ------------------------------------------------------------------------ commit


@respx.mock
async def test_files_become_one_commit_on_the_base_and_the_branch_moves_to_it(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/git/commits/{BASE}").mock(return_value=httpx.Response(200, json={"tree": {"sha": "tree0"}}))
    tree = respx.post(f"{REPO}/git/trees").mock(return_value=httpx.Response(201, json={"sha": "tree1"}))
    commit = respx.post(f"{REPO}/git/commits").mock(return_value=httpx.Response(201, json={"sha": NEW}))
    move = respx.patch(f"{REPO}/git/refs/heads/{BRANCH}").mock(return_value=httpx.Response(200, json={}))

    sha = await w.commit_files(
        TARGET,
        BRANCH,
        BASE,
        [FileWrite("src/a.py", "print(1)\r\n"), FileWrite("run.sh", "#!/bin/sh\n", executable=True), FileWrite("old.py", None)],
        "fix it",
    )

    assert sha == NEW
    entries = json.loads(tree.calls.last.request.content)["tree"]
    assert json.loads(tree.calls.last.request.content)["base_tree"] == "tree0"
    assert entries == [
        {"path": "src/a.py", "mode": "100644", "type": "blob", "content": "print(1)\r\n"},
        {"path": "run.sh", "mode": "100755", "type": "blob", "content": "#!/bin/sh\n"},
        {"path": "old.py", "mode": "100644", "type": "blob", "sha": None},
    ]
    assert json.loads(commit.calls.last.request.content) == {"message": "fix it", "tree": "tree1", "parents": [BASE]}
    assert json.loads(move.calls.last.request.content) == {"sha": NEW, "force": False}  # never a forced move


@respx.mock
async def test_nothing_to_commit_is_refused_before_any_call(writer) -> None:
    w, auth = writer

    with pytest.raises(WriteRejectedError):
        await w.commit_files(TARGET, BRANCH, BASE, [], "m")
    assert auth.asked == []


@respx.mock
async def test_a_branch_that_moved_underneath_us_is_a_rejection_not_an_overwrite(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/git/commits/{BASE}").mock(return_value=httpx.Response(200, json={"tree": {"sha": "t"}}))
    respx.post(f"{REPO}/git/trees").mock(return_value=httpx.Response(201, json={"sha": "t1"}))
    respx.post(f"{REPO}/git/commits").mock(return_value=httpx.Response(201, json={"sha": NEW}))
    respx.patch(f"{REPO}/git/refs/heads/{BRANCH}").mock(return_value=httpx.Response(422))

    with pytest.raises(WriteRejectedError):
        await w.commit_files(TARGET, BRANCH, BASE, [FileWrite("a.py", "x")], "m")


# ------------------------------------------------------------------ pull request


@respx.mock
async def test_a_draft_pull_request_is_opened_into_the_default_branch_and_labelled(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/pulls").mock(return_value=httpx.Response(200, json=[]))
    opened = respx.post(f"{REPO}/pulls").mock(return_value=httpx.Response(201, json={"number": 9, "html_url": "https://github.com/acme/shop/pull/9"}))
    label = respx.post(f"{REPO}/issues/9/labels").mock(return_value=httpx.Response(200, json=[]))

    result = await w.open_draft_pull_request(TARGET, BRANCH, "Fix", "Body")

    assert (result.number, result.url, result.created) == (9, "https://github.com/acme/shop/pull/9", True)
    body = json.loads(opened.calls.last.request.content)
    assert body["draft"] is True and body["base"] == "main" and body["head"] == BRANCH and body["maintainer_can_modify"] is False
    assert json.loads(label.calls.last.request.content) == {"labels": ["log0"]}


@respx.mock
async def test_an_existing_pull_request_is_returned_not_duplicated(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/pulls").mock(return_value=httpx.Response(200, json=[{"number": 5, "html_url": "https://github.com/acme/shop/pull/5"}]))
    create = respx.post(f"{REPO}/pulls")

    result = await w.open_draft_pull_request(TARGET, BRANCH, "Fix", "Body")

    assert (result.number, result.created) == (5, False)
    assert not create.called


@respx.mock
async def test_losing_a_race_to_open_returns_the_winners_pull_request(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/pulls").mock(
        side_effect=[httpx.Response(200, json=[]), httpx.Response(200, json=[{"number": 6, "html_url": "https://github.com/acme/shop/pull/6"}])]
    )
    respx.post(f"{REPO}/pulls").mock(return_value=httpx.Response(422))

    assert (await w.open_draft_pull_request(TARGET, BRANCH, "t", "b")).number == 6


@respx.mock
async def test_a_failed_label_does_not_fail_the_pull_request(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REPO}/pulls").mock(return_value=httpx.Response(201, json={"number": 9, "html_url": "u"}))
    respx.post(f"{REPO}/issues/9/labels").mock(return_value=httpx.Response(403))

    assert (await w.open_draft_pull_request(TARGET, BRANCH, "t", "b")).number == 9


@respx.mock
async def test_a_pull_request_nobody_can_open_is_a_rejection(writer) -> None:
    w, _ = writer
    respx.get(f"{REPO}/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REPO}/pulls").mock(return_value=httpx.Response(422))

    with pytest.raises(WriteRejectedError):
        await w.open_draft_pull_request(TARGET, BRANCH, "t", "b")


# ------------------------------------------------------------------------ errors


@respx.mock
@pytest.mark.parametrize(("status", "error"), [(403, GitHubAccessError), (404, GitHubAccessError), (401, GitHubAccessError), (500, GitHubUnavailableError)])
async def test_github_failures_use_the_package_errors(writer, status: int, error: type[Exception]) -> None:
    w, _ = writer
    respx.get(f"{REPO}/git/commits/{BASE}").mock(return_value=httpx.Response(status))

    with pytest.raises(error):
        await w.commit_files(TARGET, BRANCH, BASE, [FileWrite("a.py", "x")], "m")


@respx.mock
async def test_a_network_error_is_unavailable_and_the_token_is_not_logged(writer, caplog: pytest.LogCaptureFixture) -> None:
    w, _ = writer
    respx.get(f"{REPO}/git/ref/heads/{BRANCH}").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(GitHubUnavailableError):
        await w.ensure_branch(TARGET, BRANCH, BASE, INVESTIGATION)
    assert "ghs_write_token" not in caplog.text
