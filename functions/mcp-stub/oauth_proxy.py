"""
OAuth Proxy for the Claude Desktop / MCP OAuth2 path, plus a CIMD branch for
Claude Code (handover_claude-code-cimd-shim-test.md §1.3): when the request's
client_id is a URL instead of a GUID, it's validated against a fixed
whitelist and its own declared redirect_uris, then swapped for App Reg 2's
real client_id before falling through to the same resource/scope handling
below. Both paths remain fully stateless passthroughs — no authorization
code is minted here; Entra's native loopback redirect_uri matching (RFC 8252
§7.3, any port accepted against a portless localhost/127.0.0.1 registration)
is relied on instead of the shim tracking its own state across requests.

Why this exists: MCP clients (per RFC 8707 Resource Indicators) send a
`resource` parameter in their /authorize and /token requests, sometimes
*instead of* a `scope` rather than alongside one (confirmed against a real
Claude Desktop connection attempt, 2026-08-22 — see
Claude_Desktop_MCP_OAuth_Test_Findings.md). Microsoft Entra ID's v2.0
endpoint doesn't understand `resource` at all: sending both `resource` and
`scope` together gets rejected with AADSTS9010010 ("The resource parameter
... doesn't match with the requested scopes"), and dropping `resource`
without substituting a `scope` gets rejected with AADSTS900144 ("The
request body must contain the following parameter: 'scope'"). Both are
documented, unresolved incompatibilities between the MCP OAuth spec and
Entra's v2.0 endpoint, reproduced in several Microsoft-owned repos
(e.g. github.com/microsoft/azure-devops-mcp/issues/1293).

This proxy sits between the client and the real Entra endpoints: it always
strips `resource`, and — only when the client didn't also send a `scope` —
substitutes DEFAULT_SCOPE (this app's one named exposed scope), since
that's what `resource` was standing in for. It never sees a client secret
(the App Registration is a public client using PKCE), so it holds no
long-lived credential of its own.

Note on DEFAULT_SCOPE: it deliberately is NOT "<identifierUri>/.default".
Entra rejects the ".default" scope for an app requesting a token for
*itself* (client == resource, which is our case) unless the resource is
given as the bare client-id GUID rather than the identifierUri
(AADSTS90009: "supported only if resource is specified using the GUID
based App Identifier"), and our registered identifierUri isn't the bare
GUID (see entra_app.tf). Using the specific named scope from entra_app.tf's
`oauth2_permission_scope` sidesteps that restriction entirely. Confirmed by
reproducing both this and the .default/GUID mistake in turn, 2026-08-22.
"""

import json
import logging
import os
from urllib.parse import parse_qsl, urlencode, urlparse

import azure.functions as func
import requests

bp = func.Blueprint()

_TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "")
_DEFAULT_SCOPE = os.environ.get("DEFAULT_SCOPE", "")
_ENTRA_AUTHORIZE_URL = f"https://login.microsoftonline.com/{_TENANT_ID}/oauth2/v2.0/authorize"
_ENTRA_TOKEN_URL = f"https://login.microsoftonline.com/{_TENANT_ID}/oauth2/v2.0/token"

# CIMD (Client ID Metadata Document) support for Claude Code — handover
# §1.3: Claude Code identifies itself with a URL client_id instead of a
# GUID. Only Anthropic's fixed official document is accepted; there is no
# general-purpose URL fetch here, so this doesn't open an SSRF surface
# (decision 4, per the handover).
_CIMD_CLAUDE_CODE_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
_CLAUDE_CODE_CLIENT_ID = os.environ.get("CLAUDE_CODE_CLIENT_ID", "")
_CLAUDE_CODE_DEFAULT_SCOPE = os.environ.get("CLAUDE_CODE_DEFAULT_SCOPE", "")


def _is_cimd_client_id(client_id: str) -> bool:
    return client_id.startswith("http://") or client_id.startswith("https://")


def _redirect_uri_declared(actual: str, declared_uris: list) -> bool:
    # RFC 8252 §7.3: loopback redirect URIs may use any port, so the CIMD
    # document declares them portless. Compare scheme+host+path only —
    # Entra's own token exchange separately re-validates the real
    # ephemeral-port redirect_uri via its native loopback matching, this is
    # just gating entry into the CIMD branch.
    actual_parts = urlparse(actual)
    for candidate in declared_uris:
        candidate_parts = urlparse(candidate)
        if (
            actual_parts.scheme == candidate_parts.scheme
            and actual_parts.hostname == candidate_parts.hostname
            and actual_parts.path == candidate_parts.path
        ):
            return True
    return False


def _oauth_error(status_code: int, error: str, description: str) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": error, "error_description": description}),
        status_code=status_code,
        mimetype="application/json",
    )


def _resolve_cimd_client(params: dict):
    """Validates a CIMD client_id (whitelist + its own declared
    redirect_uris), then swaps it for App Reg 2's real client_id so the
    rest of the request can be forwarded to Entra unchanged. Returns the
    updated params dict, or an HttpResponse on validation failure."""
    client_id = params.get("client_id", "")
    if client_id != _CIMD_CLAUDE_CODE_CLIENT_ID:
        logging.warning("CIMD branch REJECTED: client_id not whitelisted (%s)", client_id)
        return _oauth_error(400, "invalid_client", "client_id is not a recognized CIMD document")

    try:
        metadata = requests.get(client_id, timeout=10).json()
    except (requests.RequestException, ValueError):
        logging.warning("CIMD branch REJECTED: failed to fetch/parse metadata document")
        return _oauth_error(400, "invalid_client", "could not fetch or parse CIMD metadata document")

    if not _redirect_uri_declared(params.get("redirect_uri", ""), metadata.get("redirect_uris", [])):
        logging.warning("CIMD branch REJECTED: redirect_uri not declared in metadata document")
        return _oauth_error(400, "invalid_request", "redirect_uri not declared in CIMD metadata document")

    logging.warning(
        "CIMD branch CONFIRMED: whitelist + redirect_uri check passed, mapping to App Reg 2 (client_id=%s)",
        _CLAUDE_CODE_CLIENT_ID,
    )
    resolved = dict(params)
    resolved["client_id"] = _CLAUDE_CODE_CLIENT_ID
    return resolved


def _strip_resource_ensure_scope(params: dict, default_scope: str) -> dict:
    out = {k: v for k, v in params.items() if k != "resource"}
    if not out.get("scope"):
        out["scope"] = default_scope
    return out


@bp.route(route="oauth/authorize", methods=["GET"])
def oauth_authorize(req: func.HttpRequest) -> func.HttpResponse:
    params = dict(req.params)
    default_scope = _DEFAULT_SCOPE

    if _is_cimd_client_id(params.get("client_id", "")):
        resolved = _resolve_cimd_client(params)
        if isinstance(resolved, func.HttpResponse):
            return resolved
        params = resolved
        default_scope = _CLAUDE_CODE_DEFAULT_SCOPE
    else:
        logging.warning("oauth/authorize: non-CIMD client_id (GUID), using Desktop's default scope")

    params = _strip_resource_ensure_scope(params, default_scope)
    location = f"{_ENTRA_AUTHORIZE_URL}?{urlencode(params)}"
    return func.HttpResponse(status_code=302, headers={"Location": location})


@bp.route(route="oauth/token", methods=["POST"])
def oauth_token(req: func.HttpRequest) -> func.HttpResponse:
    form = dict(parse_qsl(req.get_body().decode("utf-8")))
    default_scope = _DEFAULT_SCOPE

    if _is_cimd_client_id(form.get("client_id", "")):
        resolved = _resolve_cimd_client(form)
        if isinstance(resolved, func.HttpResponse):
            return resolved
        form = resolved
        default_scope = _CLAUDE_CODE_DEFAULT_SCOPE
    else:
        logging.warning("oauth/token: non-CIMD client_id (GUID), using Desktop's default scope")

    body = _strip_resource_ensure_scope(form, default_scope)

    upstream = requests.post(_ENTRA_TOKEN_URL, data=body, timeout=15)
    return func.HttpResponse(
        upstream.content,
        status_code=upstream.status_code,
        mimetype=upstream.headers.get("Content-Type", "application/json"),
    )
