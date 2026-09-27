import jwt
import pytest

from auth_context import MissingAuthContextError, extract_auth_context

# Signature is never verified by extract_auth_context, so any secret/algorithm
# works here — it's only there to produce a well-formed JWT string.
_SECRET = "test-secret-not-used-for-verification"


def _make_bearer(claims: dict) -> str:
    token = jwt.encode(claims, _SECRET, algorithm="HS256")
    return f"Bearer {token}"


def test_extracts_email_and_roles_from_valid_token():
    claims = {
        "aud": "api://2633830b-71d7-41bf-a8b7-2b8dd877bb52/mcp-test",
        "iss": "https://sts.windows.net/b7238b2a-492c-4b29-86c1-249d355a9025/",
        "email": "jason.hsiao@nextlink.com.tw",
        "roles": ["test-tier-A"],
        "scp": "access_as_user",
    }

    auth_context = extract_auth_context(_make_bearer(claims))

    assert auth_context.email == "jason.hsiao@nextlink.com.tw"
    assert auth_context.roles == ["test-tier-A"]


def test_falls_back_to_preferred_username_then_upn():
    auth_context = extract_auth_context(_make_bearer({"preferred_username": "someone@example.com"}))
    assert auth_context.email == "someone@example.com"

    auth_context = extract_auth_context(_make_bearer({"upn": "upn-user@example.com"}))
    assert auth_context.email == "upn-user@example.com"


def test_missing_authorization_header_raises():
    with pytest.raises(MissingAuthContextError):
        extract_auth_context(None)


def test_malformed_authorization_header_raises():
    with pytest.raises(MissingAuthContextError):
        extract_auth_context("NotBearer sometoken")


def test_token_without_email_like_claim_raises(caplog):
    with pytest.raises(MissingAuthContextError):
        extract_auth_context(_make_bearer({"aud": "some-aud"}))

    assert "claims keys" in caplog.text
