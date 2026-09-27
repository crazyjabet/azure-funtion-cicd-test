"""
Minimal MCP JSON-RPC server. Originally built purely so Claude Desktop's
connection handshake succeeds instead of showing a "couldn't connect / not
a valid MCP server" warning, with no real tools/resources/prompts of its
own.

2026-09 decision: Data Team will build the client's actual tools directly
on top of this module (MVP, tight timeline) rather than replacing it with a
separate implementation. `handle_jsonrpc` / `tools/list` / `tools/call`
below are the extension points for that work.

One tool, `echo`, exists purely to let a human confirm end-to-end that an
OAuth2/CIMD-authenticated client can actually reach and execute logic here —
not just complete the connection handshake (which `tools/list` returning
empty already proves). It has no business logic of its own.
"""

import datetime

_FALLBACK_PROTOCOL_VERSION = "2025-06-18"


def handle_jsonrpc(body: dict, token_aud: str = "") -> dict | None:
    """Returns a JSON-RPC response dict, or None if no response is needed
    (the message was a notification, which per JSON-RPC 2.0 has no `id`)."""
    method = body.get("method")
    msg_id = body.get("id")

    if method == "initialize":
        client_version = body.get("params", {}).get("protocolVersion", _FALLBACK_PROTOCOL_VERSION)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                # Echo back whatever version the client asked for — this
                # stub has no real capability constraints of its own to
                # negotiate down to.
                "protocolVersion": client_version,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "taitra-mcp-test-stub",
                    "version": "0.1.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Connectivity test tool: echoes back the given message along with a server-side timestamp. Confirms the authenticated MCP connection can actually execute a tool call, not just complete the connection handshake.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "message": {
                                    "type": "string",
                                    "description": "Text to echo back.",
                                },
                            },
                        },
                    },
                ],
            },
        }

    if method == "tools/call":
        params = body.get("params", {})
        if params.get("name") == "echo":
            message = params.get("arguments", {}).get("message", "")
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            text = f"echo from taitra-mcp-test-stub @ {timestamp}"
            if message:
                text += f": {message}"
            if token_aud:
                text += f" [validated token aud: {token_aud}]"
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"},
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if msg_id is None:
        # Unknown notification — nothing to reply to.
        return None

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }
