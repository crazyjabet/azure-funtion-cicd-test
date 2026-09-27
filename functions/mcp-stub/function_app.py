import datetime
import json
import logging

import azure.functions as func

from auth_context import MissingAuthContextError, extract_auth_context
from email_alias import get_member_with_alias
from hubspot_webhook import bp as hubspot_webhook_bp
from mcp_server import handle_jsonrpc
from member_repository import get_member_repository
from oauth_proxy import bp as oauth_proxy_bp
from quota_service import check_and_increment

# Anonymous by design — auth for /mcp/* is handled entirely by the APIM
# policy in front of it (validate-azure-ad-token on /mcp-test); /oauth/* is
# itself the auth-token plumbing, so it can't require a token to call it.
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Module-level so the Cosmos-backed implementation (once it exists) reuses
# one connection across requests instead of reconnecting every call.
_member_repo = get_member_repository()


# Scoped under "mcp/" (not a bare "{*route}") specifically so it can never
# collide with "oauth/*" below — Azure Functions' HTTP routing picks
# whichever registered template is *shorter* on overlap, regardless of
# specificity (https://github.com/Azure/azure-functions-host/issues/9876),
# so two catch-alls or a catch-all sharing a prefix with a literal route is
# a real, easy-to-hit trap. Giving each concern its own first path segment
# sidesteps that class of bug entirely instead of working around it.
@app.route(route="mcp/{*route}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def mcp_stub(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "GET":
        # MCP Streamable HTTP transport: GET opens an optional SSE stream
        # for server-initiated messages. This stub never initiates
        # anything, so 405 is the spec-sanctioned way to say "not
        # supported" — clients are expected to treat that as fine, not
        # fatal, and fall back to request/response only.
        return func.HttpResponse(status_code=405)

    if req.method == "POST":
        try:
            body = req.get_json()
        except ValueError:
            body = None

        if isinstance(body, dict) and body.get("jsonrpc") == "2.0":
            try:
                auth_context = extract_auth_context(req.headers.get("Authorization"))
            except MissingAuthContextError as e:
                logging.error("身分解析失敗: %s", e)
                # 正常情況不應該發生（APIM 已擋掉無 token 請求），發生時代表
                # Function 被繞過 APIM 直接呼叫，或 APIM policy 設定有誤。
                # 詳細的告警/稽核機制留待後續任務。
                return func.HttpResponse(
                    json.dumps({"error": "missing_identity"}),
                    status_code=401,
                    mimetype="application/json",
                )

            member = get_member_with_alias(_member_repo, auth_context.email)
            logging.info(
                "member lookup: email=%s tier=%s source=%s",
                auth_context.email,
                member.tier if member else None,
                member.source if member else None,
            )

            # 只有 tools/call 算「使用」——initialize/tools/list/ping 這些是
            # protocol handshake，不是在消耗會員的額度，這是技術判斷（可以從
            # mcp_server.py 現狀推導：目前唯一的 tool 是 echo，沒有接真正的
            # AI 呼叫），不是本輪新發明的業務規則。
            if body.get("method") == "tools/call":
                business_number = member.business_number if member else None
                if business_number is None:
                    # 查不到所屬公司（MockMemberRepository 的預設 fallback、
                    # 或 CosmosMemberRepository 查不到 credentials/company
                    # 關聯時都會是 None）——無法對到 quota_usage 的
                    # partition key，比照本檔案既有「這輪先不擋、只記
                    # log」的保守慣例，不因為配額查不到而拒絕請求。
                    logging.warning(
                        "quota check skipped: email=%s has no business_number, cannot track quota",
                        auth_context.email,
                    )
                else:
                    result = check_and_increment(business_number, member.tier)
                    logging.info(
                        "quota check: business_number=%s tier=%s used=%s quota=%s allowed=%s",
                        business_number,
                        result.tier,
                        result.used,
                        result.quota,
                        result.allowed,
                    )
                    if not result.allowed:
                        return func.HttpResponse(
                            json.dumps(
                                {
                                    "error": "quota_exceeded",
                                    "tier": result.tier,
                                    "quota": result.quota,
                                    "used": result.used,
                                }
                            ),
                            status_code=429,
                            mimetype="application/json",
                        )

            token_aud = req.headers.get("X-Debug-Token-Aud", "")
            response = handle_jsonrpc(body, token_aud)
            if response is None:
                # Notification — no response body per JSON-RPC 2.0.
                return func.HttpResponse(status_code=202)
            return func.HttpResponse(
                json.dumps(response),
                status_code=200,
                mimetype="application/json",
            )

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return func.HttpResponse(f"MCP server stub OK - {timestamp}", status_code=200)


app.register_functions(oauth_proxy_bp)
app.register_functions(hubspot_webhook_bp)
