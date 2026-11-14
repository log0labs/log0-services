"""Step 5.5: the /mcp endpoint of the real app: off by default, bearer-token only, tenant from the token."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from tests.conftest import requires_postgres
from tests.jwt_helpers import TENANT_A, TENANT_B, mint_access_token
from tests.memory_support import make_investigation, wipe_investigations

ACCEPT = "application/json, text/event-stream"


def token(tenant=TENANT_A, role="VIEWER", **kw) -> str:
    return mint_access_token(get_settings().jwt_secret.get_secret_value(), tenant_id=tenant, role=role, **kw)


def headers(tok: str | None) -> dict[str, str]:
    h = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def rpc(client: TestClient, method: str, params: dict | None = None, tok: str | None = None, *, id_: int = 1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers(tok), json=body)


def tool_call(client: TestClient, name: str, args: dict, tok: str):
    response = rpc(client, "tools/call", {"name": name, "arguments": args}, tok)
    assert response.status_code == 200, response.text
    return response.json()["result"]


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
    get_settings.cache_clear()
    wipe_investigations()
    with TestClient(create_app()) as client:
        yield client
    wipe_investigations()


def test_the_endpoint_is_off_unless_switched_on():
    with TestClient(create_app()) as client:
        assert rpc(client, "tools/list", None, token()).status_code == 404
        assert client.get("/health").status_code == 200  # the rest of the app is unaffected


def test_a_request_without_a_valid_token_is_refused(enabled):
    for tok in (None, "garbage", token(exp_delta=timedelta(seconds=-60)), token(role="HACKER")):
        response = rpc(enabled, "tools/list", None, tok)
        assert response.status_code == 401, tok
        assert response.headers["www-authenticate"] == "Bearer"
    wrong_secret = mint_access_token("another-secret-that-is-at-least-32-characters", tenant_id=TENANT_A, role="ADMIN")
    assert rpc(enabled, "tools/list", None, wrong_secret).status_code == 401


def test_a_basic_auth_header_is_not_a_bearer_token(enabled):
    response = enabled.post(
        "/mcp", headers={**headers(None), "Authorization": f"Basic {token()}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert response.status_code == 401


def test_a_viewer_can_initialize_and_list_the_ten_read_only_tools(enabled):
    tok = token(role="VIEWER")
    init = rpc(
        enabled,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        tok,
    )
    assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "log0"
    tools = rpc(enabled, "tools/list", None, tok).json()["result"]["tools"]
    assert len(tools) == 10 and all(t["annotations"]["readOnlyHint"] for t in tools)


def test_the_rest_of_the_api_still_works_with_mcp_on(enabled):
    response = enabled.get("/api/v1/runbooks", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 200


@requires_postgres
def test_each_request_sees_only_its_own_tenant_even_back_to_back(enabled):
    mine, _ = make_investigation(TENANT_A)
    theirs, _ = make_investigation(TENANT_B)
    a, b = token(TENANT_A), token(TENANT_B)

    found = tool_call(enabled, "get_investigation", {"investigation_id": str(mine)}, a)
    assert not found["isError"] and json.loads(found["content"][0]["text"])["status"] == "COMPLETED"

    cross = tool_call(enabled, "get_investigation", {"investigation_id": str(mine)}, b)
    assert cross["isError"] and "not found" in cross["content"][0]["text"]

    own = tool_call(enabled, "get_investigation", {"investigation_id": str(theirs)}, b)
    assert not own["isError"]
    again = tool_call(enabled, "get_investigation", {"investigation_id": str(theirs)}, a)
    assert again["isError"]  # tenant A's identity did not stick to the connection


@requires_postgres
def test_runbook_search_over_http_is_tenant_scoped(enabled):
    admin_a = {"Authorization": f"Bearer {token(TENANT_A, 'ADMIN')}"}
    enabled.post("/api/v1/runbooks", headers=admin_a, json={"title": "Pool", "markdown": "# Pool\n\nWhen the connection pool is exhausted, raise the pool size."})
    try:
        hit = tool_call(enabled, "search_runbooks", {"query": "connection pool exhausted"}, token(TENANT_A))
        payload = json.loads(hit["content"][0]["text"])
        assert payload["outcome"] == "found" and payload["passages"][0]["runbook"] == "Pool"
        other = tool_call(enabled, "search_runbooks", {"query": "connection pool exhausted"}, token(TENANT_B))
        assert json.loads(other["content"][0]["text"])["outcome"] == "no_runbooks"
    finally:
        from tests.runbook_support import wipe_runbooks

        wipe_runbooks()


def test_an_unknown_tool_and_bad_arguments_are_clean_errors(enabled):
    tok = token()
    bad_tool = rpc(enabled, "tools/call", {"name": "delete_everything", "arguments": {}}, tok)
    assert bad_tool.status_code == 200 and (bad_tool.json().get("error") or bad_tool.json()["result"]["isError"])
    bad_args = tool_call(enabled, "get_incident", {"incident_id": str(uuid4()), "tenant_id": str(TENANT_B)}, tok)
    assert bad_args["isError"]  # a tenant argument is not even accepted
