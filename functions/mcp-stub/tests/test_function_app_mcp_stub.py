import json
import logging

import azure.functions as func
import jwt
import pytest

import function_app
from quota_service import QuotaCheckResult

_SECRET = "test-secret-not-used-for-verification"
_VALID_CLAIMS = {
    "aud": "api://2633830b-71d7-41bf-a8b7-2b8dd877bb52/mcp-test",
    "email": "known-paid-member@example.com",
    "roles": ["test-tier-A"],
}


def _bearer(claims: dict = _VALID_CLAIMS) -> str:
    return "Bearer " + jwt.encode(claims, _SECRET, algorithm="HS256")


def _jsonrpc_request(payload: dict, headers: dict | None = None) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="/mcp/",
        headers=headers or {},
        body=json.dumps(payload).encode("utf-8"),
    )


def test_missing_authorization_returns_401_not_500():
    req = _jsonrpc_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    resp = function_app.mcp_stub(req)

    assert resp.status_code == 401
    assert json.loads(resp.get_body()) == {"error": "missing_identity"}


@pytest.mark.parametrize(
    "method,params",
    [
        ("initialize", {"protocolVersion": "2026-06-18"}),
        ("tools/list", {}),
        ("ping", {}),
    ],
)
def test_valid_token_leaves_handshake_methods_unaffected(caplog, method, params):
    req = _jsonrpc_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Authorization": _bearer()},
    )

    with caplog.at_level(logging.INFO):
        resp = function_app.mcp_stub(req)

    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert "error" not in body

    assert "member lookup: email=known-paid-member@example.com tier=paid source=mock" in caplog.text


def test_get_still_returns_405():
    req = func.HttpRequest(method="GET", url="/mcp/", headers={}, body=b"")
    resp = function_app.mcp_stub(req)
    assert resp.status_code == 405


def test_non_jsonrpc_post_unaffected_by_auth_check():
    req = func.HttpRequest(
        method="POST",
        url="/mcp/",
        headers={},
        body=b"not json-rpc",
    )
    resp = function_app.mcp_stub(req)
    assert resp.status_code == 200
    assert b"MCP server stub OK" in resp.get_body()


def _tools_call_request(email: str = "known-paid-member@example.com") -> func.HttpRequest:
    return _jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"message": "hi"}},
        },
        headers={"Authorization": _bearer({**_VALID_CLAIMS, "email": email})},
    )


def test_tools_call_checks_quota_and_proceeds_when_allowed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        function_app,
        "check_and_increment",
        lambda business_number, tier: calls.append((business_number, tier))
        or QuotaCheckResult(allowed=True, tier=tier, quota=10, used=1),
    )

    resp = function_app.mcp_stub(_tools_call_request())

    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert "error" not in body
    # known-paid-member@example.com is MockMemberRepository's known paid-tier
    # fixture, with business_number="mock-business-number" (see
    # member_repository.py).
    assert calls == [("mock-business-number", "paid")]


def test_tools_call_returns_429_when_quota_exceeded_and_does_not_invoke_the_tool(monkeypatch):
    monkeypatch.setattr(
        function_app,
        "check_and_increment",
        lambda business_number, tier: QuotaCheckResult(allowed=False, tier=tier, quota=10, used=10),
    )
    handle_jsonrpc_calls = []
    monkeypatch.setattr(
        function_app,
        "handle_jsonrpc",
        lambda body, token_aud: handle_jsonrpc_calls.append(body) or {"jsonrpc": "2.0", "id": 1, "result": {}},
    )

    resp = function_app.mcp_stub(_tools_call_request())

    assert resp.status_code == 429
    assert json.loads(resp.get_body()) == {"error": "quota_exceeded", "tier": "paid", "quota": 10, "used": 10}
    assert handle_jsonrpc_calls == []  # rejected before the tool ever runs


@pytest.mark.parametrize(
    "method,params",
    [
        ("initialize", {"protocolVersion": "2026-06-18"}),
        ("tools/list", {}),
        ("ping", {}),
    ],
)
def test_non_tools_call_methods_never_check_quota(monkeypatch, method, params):
    def _fail_if_called(business_number, tier):
        raise AssertionError("quota should not be checked for non-tools/call methods")

    monkeypatch.setattr(function_app, "check_and_increment", _fail_if_called)

    req = _jsonrpc_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Authorization": _bearer()},
    )
    resp = function_app.mcp_stub(req)

    assert resp.status_code == 200


def test_tools_call_skips_quota_check_when_member_has_no_business_number(monkeypatch, caplog):
    def _fail_if_called(business_number, tier):
        raise AssertionError("quota should not be checked when there is no business_number to key it by")

    monkeypatch.setattr(function_app, "check_and_increment", _fail_if_called)

    # Not MockMemberRepository's known fixture email, so it falls back to
    # free tier with business_number=None (see member_repository.py).
    with caplog.at_level(logging.WARNING):
        resp = function_app.mcp_stub(_tools_call_request(email="nobody@example.com"))

    assert resp.status_code == 200
    assert "quota check skipped" in caplog.text
