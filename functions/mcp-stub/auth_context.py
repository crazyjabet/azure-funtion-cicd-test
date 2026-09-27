"""
從已經過 APIM validate-azure-ad-token 驗證的請求中，解出會員身分。

重要：這裡只 decode JWT payload，不重新驗證簽章。
簽章／audience／issuer 的驗證責任在 APIM 的 validate-azure-ad-token policy，
本函式的前提是「這支 Function 只會被 APIM 呼叫」。這個前提目前（PoC 階段）
還沒有用 Infra 手段鎖死（Private Endpoint 尚未落地），屬於已知、已記錄的
暫時性風險，見 handover_functions-jwt-email-cosmosdb-mock.md 第 3.1 節。
"""

import logging
from dataclasses import dataclass

import jwt  # PyJWT

logger = logging.getLogger(__name__)


class MissingAuthContextError(Exception):
    """Authorization header 不存在，或 token 裡找不到可用的身分 claim。"""


@dataclass(frozen=True)
class AuthContext:
    email: str
    roles: list[str]


def extract_auth_context(authorization_header: str | None) -> AuthContext:
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        raise MissingAuthContextError("Missing or malformed Authorization header")

    token = authorization_header.split(" ", 1)[1].strip()

    # 只 decode，不驗簽章：APIM 已經驗證過這把 token。
    # verify_signature / verify_aud / verify_exp 皆關閉，因為這裡沒有簽章金鑰、
    # 也不需要重複驗證 APIM 已經做過的事。
    claims = jwt.decode(
        token,
        options={
            "verify_signature": False,
            "verify_aud": False,
            "verify_exp": False,
        },
    )

    # 依實測（見 Claude_Desktop_MCP_OAuth_Test_Findings.md）Workforce tenant 的
    # token 使用 "email" claim。正式環境換 Entra External ID (CIAM) 後，這個
    # claim 名稱是否一致待重新驗證，保留 preferred_username / upn 作為
    # fallback，並明確記 log 方便之後排查。
    email = claims.get("email") or claims.get("preferred_username") or claims.get("upn")
    if not email:
        logger.error("Token 中找不到 email/preferred_username/upn claim，claims keys=%s", list(claims.keys()))
        raise MissingAuthContextError("Token does not contain an email-like claim")

    roles = claims.get("roles", [])

    return AuthContext(email=email, roles=roles)
